import json
import time

import pytest

from qwenbench.hero import hooks, override
from qwenbench.hero.hooks import decide_pre_tool_use
from qwenbench.hero.ledger import read_jsonl


def ev(project, tool, agent_type=None, **tool_input):
    e = {"tool_name": tool, "tool_input": tool_input, "cwd": str(project), "session_id": "s1"}
    if agent_type:
        e["agent_type"] = agent_type
    return e


def test_spawning_qwen_role_is_denied_with_dispatch_instructions(cfg, configured_project):
    d = decide_pre_tool_use(ev(configured_project, "Agent", subagent_type="engineer"), cfg.policy, configured_project)
    assert not d.allow and d.rule == "agent-qwen-must-dispatch"
    assert "qwenbench dispatch" in d.reason and "do not implement" in d.reason.lower()
    assert d.route.model_id == "qwen:a6000"


def test_legacy_task_tool_and_namespaced_agent(cfg, configured_project):
    d = decide_pre_tool_use(ev(configured_project, "Task", subagent_type="hero:api-engineer"), cfg.policy,
                            configured_project)
    assert not d.allow


def test_frontier_roles_allowed(cfg, configured_project):
    for agent in ("brownfield-architect", "feature-delivery-lead", "pr-reviewer", "Explore"):
        d = decide_pre_tool_use(ev(configured_project, "Agent", subagent_type=agent), cfg.policy, configured_project)
        assert d.allow, agent


def test_general_purpose_and_unknown_denied(cfg, configured_project):
    for agent in ("general-purpose", "totally-new-agent"):
        d = decide_pre_tool_use(ev(configured_project, "Agent", subagent_type=agent), cfg.policy, configured_project)
        assert not d.allow


def test_agent_without_type_defaults_to_general_purpose_and_is_denied(cfg, configured_project):
    d = decide_pre_tool_use(ev(configured_project, "Agent", prompt="do it"), cfg.policy, configured_project)
    assert not d.allow


def test_main_thread_cannot_edit_implementation(cfg, configured_project):
    p = configured_project
    for tool in ("Edit", "Write", "MultiEdit"):
        d = decide_pre_tool_use(ev(p, tool, file_path=str(p / "src/app.py")), cfg.policy, p)
        assert not d.allow and d.rule == "edit-impl-denied"
    d = decide_pre_tool_use(ev(p, "NotebookEdit", notebook_path="nb/analysis.ipynb"), cfg.policy, p)
    assert not d.allow


def test_frontier_may_write_specs_and_docs(cfg, configured_project):
    p = configured_project
    for path in (".hero/specs/feat/spec.md", "docs/adr/0001.md", "README.md", "notes/design.md"):
        d = decide_pre_tool_use(ev(p, "Write", file_path=str(p / path)), cfg.policy, p)
        assert d.allow, path


def test_design_subagent_cannot_edit_code_either(cfg, configured_project):
    p = configured_project
    d = decide_pre_tool_use(ev(p, "Edit", agent_type="feature-delivery-lead", file_path="src/app.py"), cfg.policy, p)
    assert not d.allow


def test_protected_paths_denied_even_with_override(cfg, configured_project, monkeypatch):
    p = configured_project
    monkeypatch.setattr(override, "find_active", lambda *a, **k: object())
    for path in (".claude/settings.local.json", ".hero/hero.local.json", ".qwen-routing/config.json"):
        d = decide_pre_tool_use(ev(p, "Edit", file_path=str(p / path)), cfg.policy, p)
        assert not d.allow and d.rule == "protected-path", path


def test_edit_outside_project_not_governed(cfg, configured_project, tmp_path):
    d = decide_pre_tool_use(ev(configured_project, "Write", file_path=str(tmp_path / "scratch.txt")), cfg.policy,
                            configured_project)
    assert d.allow


@pytest.mark.parametrize("cmd,allowed", [
    ("pytest -q", True),
    ("qwenbench dispatch --role engineer --task-file t.md", True),
    ("git status && git diff", True),
    ("echo hi > docs/notes.md", True),
    ("ls 2>/dev/null", True),
    ("echo 'x' > src/app.py", False),
    ("cat patch.txt | tee src/app.py", False),
    ("sed -i '' 's/a - b/a + b/' src/app.py", False),
    ("git apply fix.patch", False),
    ("patch -p1 < fix.diff", False),
    ("git checkout HEAD -- src/app.py", False),
    ("qwenbench override grant --role engineer --reason x", False),
    ("echo x > .claude/settings.local.json", False),
])
def test_bash_rules(cfg, configured_project, cmd, allowed):
    d = decide_pre_tool_use(ev(configured_project, "Bash", command=cmd), cfg.policy, configured_project)
    assert d.allow is allowed, (cmd, d.rule, d.reason)


def test_human_override_allows_and_expires(cfg, configured_project):
    p = configured_project
    override.grant(p, "engineer", "agent", "Qwen endpoint down during demo", ttl_s=60, granted_by="dave")
    d = decide_pre_tool_use(ev(p, "Agent", subagent_type="engineer"), cfg.policy, p)
    assert d.allow and d.rule == "agent-override"
    # other roles still enforced
    assert not decide_pre_tool_use(ev(p, "Agent", subagent_type="api-engineer"), cfg.policy, p).allow
    # the override is scoped to spawning; editing code still denied
    assert not decide_pre_tool_use(ev(p, "Edit", file_path="src/app.py"), cfg.policy, p).allow
    items = override.load(p)
    items[0].expires_at = time.time() - 1
    override._save(p, items)
    assert not decide_pre_tool_use(ev(p, "Agent", subagent_type="engineer"), cfg.policy, p).allow
    assert any(r.get("event") == "override-granted" for r in read_jsonl(p, "decisions.jsonl"))


def test_edits_override(cfg, configured_project):
    p = configured_project
    override.grant(p, "*", "edits", "hotfix at 2am", ttl_s=60, granted_by="dave")
    assert decide_pre_tool_use(ev(p, "Edit", file_path="src/app.py"), cfg.policy, p).allow


def test_hook_main_noop_when_not_configured(hero_project, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(hero_project))
    code = hooks.main("pre-tool-use", json.dumps(ev(hero_project, "Agent", subagent_type="engineer")))
    assert code == 0 and capsys.readouterr().out == ""


def test_hook_main_denies_with_exit_2_and_json(configured_project, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(configured_project))
    code = hooks.main("pre-tool-use", json.dumps(ev(configured_project, "Agent", subagent_type="engineer")))
    out = capsys.readouterr()
    assert code == 2
    payload = json.loads(out.out)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "qwenbench dispatch" in out.err
    log = read_jsonl(configured_project, "decisions.jsonl")
    assert log[-1]["allow"] is False and log[-1]["subagent"] == "engineer"


def test_hook_fails_closed_on_broken_config(configured_project, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(configured_project))
    import qwenbench.config as c
    monkeypatch.setattr(c, "load_config", lambda: (_ for _ in ()).throw(ValueError("bad yaml")))
    code = hooks.main("pre-tool-use", json.dumps(ev(configured_project, "Edit", file_path="src/app.py")))
    assert code == 2 and "refusing" in capsys.readouterr().err


def test_session_start_injects_policy_and_records_baseline(configured_project, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(configured_project))
    assert hooks.main("session-start", json.dumps({"session_id": "s9", "cwd": str(configured_project)})) == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "qwenbench dispatch" in ctx and "engineer" in ctx
    assert (configured_project / ".qwen-routing" / "baseline.json").exists()


def test_stop_hook_flags_unattributed_code_changes(configured_project, capsys, monkeypatch):
    p = configured_project
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(p))
    hooks.main("session-start", json.dumps({"session_id": "s", "cwd": str(p)}))
    capsys.readouterr()
    (p / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")  # e.g. via a shell trick
    (p / "docs").mkdir()
    (p / "docs" / "note.md").write_text("fine")  # spec/doc writes are allowed
    hooks.main("stop", json.dumps({"session_id": "s", "cwd": str(p)}))
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "src/app.py" in msg and "docs/note.md" not in msg


def test_hook_command_never_resolves_qwen_via_path(monkeypatch):
    """Other tools install a `qwen` executable (Qwen Code does); hooks must not call it."""
    import shlex
    import sys

    from qwenbench.hero.configure import hook_command
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    argv = shlex.split(hook_command())
    assert argv[:3] == [sys.executable, "-m", "qwenbench.cli.main"] and argv[3] == "hook"


def test_bash_blocks_new_executable_name_for_overrides(cfg, configured_project):
    d = decide_pre_tool_use(ev(configured_project, "Bash", command="qwenbench override grant --role engineer"),
                            cfg.policy, configured_project)
    assert not d.allow and d.rule == "override-human-only"
