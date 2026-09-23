"""The committed infrastructure definition must match config (IaC drift check)."""

import json
import subprocess

from qwenbench.cli.infra import render_profile
from qwenbench.paths import repo_root


def test_rendered_infra_matches_config(cfg):
    for name in cfg.profile_names():
        path = repo_root() / "infra" / "runpod" / "rendered" / f"{name}.json"
        assert path.exists(), f"missing {path}; run `qwen infra render`"
        assert json.loads(path.read_text()) == render_profile(cfg, name), f"{path} is stale; run `qwen infra render`"


def test_no_secrets_in_rendered_infra(cfg):
    for path in (repo_root() / "infra").rglob("*.json"):
        text = path.read_text()
        assert "rpa_" not in text and "hf_" not in text.replace("hf_cache", "")


def test_env_file_is_ignored():
    r = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=repo_root())
    assert r.returncode == 0, ".env must be git-ignored"
    r = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=repo_root())
    assert r.returncode == 1, ".env.example must be committed"
