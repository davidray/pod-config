from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from qwenbench.config import load_config

FAKE_RUNPOD_KEY = "rpa_FAKE_TEST_KEY_do_not_leak_0123456789"
FAKE_HF_TOKEN = "hf_FAKE_TEST_TOKEN_do_not_leak_0123456789"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Never touch the real state dir, results dir, or credentials."""
    monkeypatch.setenv("QWEN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QWEN_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("RUNPOD_API_KEY", FAKE_RUNPOD_KEY)
    monkeypatch.setenv("HF_TOKEN", FAKE_HF_TOKEN)
    import qwenbench.secrets as s
    monkeypatch.setattr(s, "_dotenv_cache", {})
    yield


@pytest.fixture
def cfg():
    return load_config()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def hero_project(tmp_path) -> Path:
    """A git repo shaped like a Hero-managed project (hero v0.34.1 layout)."""
    proj = tmp_path / "proj"
    (proj / ".hero").mkdir(parents=True)
    (proj / ".claude" / "agents").mkdir(parents=True)
    (proj / "src").mkdir()
    (proj / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (proj / "CLAUDE.md").write_text("<!-- hero:managed-start v=v0.34.1 -->\nhero stuff\n<!-- hero:managed-end -->\n")
    (proj / ".hero" / "hero.json").write_text(json.dumps({
        "folder": ".hero", "team": {"require_review": False}, "tracker": {"type": "none"}}, indent=2))
    (proj / ".hero" / "hero.local.json").write_text(json.dumps({
        "integrations": {"connections": {"gh": {"auth": {"token": "ghp_localsecret_1234567890"}}}}}, indent=2))
    agents = ["engineer", "api-engineer", "brownfield-architect", "feature-delivery-lead", "pr-reviewer"]
    for a in agents:
        (proj / ".claude" / "agents" / f"{a}.md").write_text(f"---\nname: {a}\nmode: subagent\n---\nbody\n")
    (proj / ".hero" / "install-state.json").write_text(json.dumps({
        "hero_version": "v0.34.1",
        "targets": {"claude": {"files": [f".claude/agents/{a}.md" for a in agents]}}}))
    (proj / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"command": "hero next checkpoint --quiet", "type": "command"}], "matcher": ""}]}}))
    git(proj, "init", "-q", "-b", "main")
    git(proj, "add", "-A")
    git(proj, "commit", "-qm", "init")
    return proj


@pytest.fixture
def configured_project(hero_project, cfg) -> Path:
    from qwenbench.hero import configure as conf

    conf.apply(conf.plan_configure(cfg, hero_project, "a6000", "claude-opus-5-5"))
    return hero_project
