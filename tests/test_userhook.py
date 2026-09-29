"""The user-level gate enforces in every checkout of a configured repo, and nowhere else.

Reproduces the first live trial: the session ran in a fresh worktree that had
none of the configured checkout's untracked files, so per-checkout hooks never ran.
"""

import json
import os
import subprocess
from pathlib import Path

from qwenbench.hero import userhook


def run_gate(kind, event, project_dir=None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if project_dir:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    env.update({k: v for k, v in os.environ.items() if k.startswith("QWEN")})
    return subprocess.run([str(userhook.wrapper_path()), kind], input=json.dumps(event), capture_output=True,
                          text=True, env=env, timeout=60)


def fresh_worktree(repo, tmp_path, name="wt"):
    wt = tmp_path / name
    subprocess.run(["git", "worktree", "add", "-q", "-b", name, str(wt)], cwd=repo, check=True)
    (wt / ".hero" / "hero.local.json").unlink(missing_ok=True)  # git-ignored in real Hero projects
    assert not (wt / ".qwen-routing").exists()
    return wt


def test_fresh_worktree_of_a_configured_repo_is_enforced(configured_project, tmp_path):
    wt = fresh_worktree(configured_project, tmp_path)
    spawn = {"tool_name": "Agent", "tool_input": {"subagent_type": "engineer"}, "cwd": str(wt), "session_id": "s1"}
    p = run_gate("pre-tool-use", spawn)
    assert p.returncode == 2 and "qwenbench dispatch" in p.stderr
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(wt / "src" / "app.py")}, "cwd": str(wt),
            "session_id": "s1"}
    assert run_gate("pre-tool-use", edit).returncode == 2
    spec = {"tool_name": "Write", "tool_input": {"file_path": str(wt / ".hero" / "planning" / "x.md")},
            "cwd": str(wt), "session_id": "s1"}
    assert run_gate("pre-tool-use", spec).returncode == 0


def test_session_that_started_elsewhere_is_judged_by_where_it_works(configured_project, tmp_path):
    other = tmp_path / "unbound"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)
    spawn = {"tool_name": "Agent", "tool_input": {"subagent_type": "engineer"}, "cwd": str(configured_project),
             "session_id": "s2"}
    assert run_gate("pre-tool-use", spawn, project_dir=other).returncode == 2


def test_unconfigured_repo_is_a_fast_no_op(tmp_path):
    userhook.install()
    repo = tmp_path / "plain"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    spawn = {"tool_name": "Agent", "tool_input": {"subagent_type": "engineer"}, "cwd": str(repo), "session_id": "s3"}
    p = run_gate("pre-tool-use", spawn)
    assert p.returncode == 0 and p.stdout == "" and not (repo / ".qwen-routing").exists()


def test_claude_cannot_edit_the_binding_or_the_user_hooks(configured_project, tmp_path):
    wt = fresh_worktree(configured_project, tmp_path)
    # The gate is a separate process, so it guards the real ~/.claude/settings.json (a decision only; nothing is written).
    for target in (configured_project / ".git" / "qwenbench" / "config.json", Path.home() / ".claude" / "settings.json",
                   userhook.wrapper_path()):
        ev = {"tool_name": "Write", "tool_input": {"file_path": str(target)}, "cwd": str(wt), "session_id": "s4"}
        assert run_gate("pre-tool-use", ev).returncode == 2, target
    for cmd in ("qwenbench hero unconfigure .", "qwenbench hero uninstall-hooks",
                f"echo '{{}}' > {configured_project / '.git' / 'qwenbench' / 'config.json'}"):
        ev = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(wt), "session_id": "s4"}
        assert run_gate("pre-tool-use", ev).returncode == 2, cmd


def test_install_keeps_other_settings_and_uninstall_removes_only_ours(tmp_path):
    s = userhook.settings_path()
    s.parent.mkdir(parents=True, exist_ok=True)
    s.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": "hero next checkpoint --quiet"}]}]}}))
    userhook.install()
    userhook.install()
    data = json.loads(s.read_text())
    assert data["model"] == "opus" and userhook.installed()
    assert len([h for h in data["hooks"]["Stop"] if h.get("added_by") == "qwenbench"]) == 1
    userhook.uninstall()
    data = json.loads(s.read_text())
    assert data == {"model": "opus", "hooks": {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": "hero next checkpoint --quiet"}]}]}}
    assert not userhook.installed()
