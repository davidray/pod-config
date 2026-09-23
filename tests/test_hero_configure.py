import hashlib
import json
import shutil
import subprocess

import pytest

from qwenbench.hero import configure as conf


def digest(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def hero_owned(project):
    return [*sorted((project / ".claude" / "agents").glob("*.md")), project / "CLAUDE.md",
            project / ".hero" / "hero.json", project / ".claude" / "settings.json"]


def test_plan_shows_diff_and_preserves_unrelated_settings(cfg, hero_project):
    plan = conf.plan_configure(cfg, hero_project, "a6000", "claude-opus-5-5")
    diff = plan.diff()
    assert '"execution": "qwen:a6000"' in diff
    assert "hero.local.json" in diff and "settings.local.json" in diff and "CLAUDE.local.md" in diff
    before = digest(hero_owned(hero_project))
    conf.apply(plan)
    local = json.loads((hero_project / ".hero" / "hero.local.json").read_text())
    assert local["models"]["roles"] == {"design": "claude-opus-5-5", "execution": "qwen:a6000",
                                        "review": "claude-opus-5-5"}
    assert local["integrations"]["connections"]["gh"]["auth"]["token"] == "ghp_localsecret_1234567890"
    # Hero-owned files are untouched (hero upgrade cannot clobber our changes).
    assert digest(hero_owned(hero_project)) == before


def test_configure_is_idempotent(cfg, configured_project):
    assert conf.plan_configure(cfg, configured_project, "a6000", "claude-opus-5-5").diff() == ""


def test_switching_profile_changes_only_the_execution_role(cfg, configured_project):
    diff = conf.plan_configure(cfg, configured_project, "l40s", "claude-opus-5-5").diff()
    assert '-      "execution": "qwen:a6000"' in diff and '+      "execution": "qwen:l40s"' in diff


def test_user_hooks_preserved_and_ours_replaced(cfg, hero_project):
    s = hero_project / ".claude" / "settings.local.json"
    s.write_text(json.dumps({"permissions": {"allow": ["Bash(make:*)"]},
                             "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                                   "command": "my-guard"}]}]}}))
    for _ in range(2):
        conf.apply(conf.plan_configure(cfg, hero_project, "a6000", "claude-opus-5-5"))
    data = json.loads(s.read_text())
    pre = data["hooks"]["PreToolUse"]
    assert [h["hooks"][0]["command"] for h in pre if h.get("added_by") != "qwenbench"] == ["my-guard"]
    assert len([h for h in pre if h.get("added_by") == "qwenbench"]) == 1
    assert data["permissions"]["allow"] == ["Bash(make:*)"]


def test_claude_local_block_not_duplicated(cfg, hero_project):
    (hero_project / "CLAUDE.local.md").write_text("# my notes\n")
    conf.apply(conf.plan_configure(cfg, hero_project, "a6000", "claude-opus-5-5"))
    conf.apply(conf.plan_configure(cfg, hero_project, "l40s", "claude-opus-5-5"))
    text = (hero_project / "CLAUDE.local.md").read_text()
    assert text.startswith("# my notes\n") and text.count(conf.BLOCK_START) == 1 and "qwen:l40s" in text


def test_git_exclude_keeps_local_files_out_of_git(cfg, configured_project):
    status = subprocess.run(["git", "status", "--porcelain"], cwd=configured_project, capture_output=True,
                            text=True).stdout
    assert ".qwen-routing" not in status and "CLAUDE.local.md" not in status and "settings.local.json" not in status


def test_inspect_reports_inventory_and_unclassified(cfg, configured_project):
    (configured_project / ".claude" / "agents" / "my-custom-agent.md").write_text("---\nname: x\n---\n")
    info = conf.inspect(cfg, configured_project)
    assert "engineer" in info["agents"]["hero_owned"]
    assert info["agents"]["user_agents"] == ["my-custom-agent"]
    assert info["unclassified_agents"] == ["my-custom-agent"]
    assert info["hooks_installed"] and info["routing_configured"]


def test_simulated_policy_all_pass(cfg, configured_project):
    results = conf.simulate(cfg, configured_project)
    assert all(r["ok"] for r in results), [r for r in results if not r["ok"]]


@pytest.mark.skipif(shutil.which("hero") is None, reason="hero not installed")
def test_real_hero_reads_the_configured_roles(cfg, tmp_path):
    """Validate with Hero's own tooling: `hero models` must show our mapping."""
    proj = tmp_path / "real"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    r = subprocess.run(["hero", "init", "--no-hooks", "--no-agents"], cwd=proj, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"hero init failed here: {r.stderr[-300:]}")
    conf.apply(conf.plan_configure(cfg, proj, "a6000", "claude-opus-5-5"))
    out = subprocess.run(["hero", "models", "--check"], cwd=proj, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "qwen:a6000" in out.stdout and "claude-opus-5-5" in out.stdout
