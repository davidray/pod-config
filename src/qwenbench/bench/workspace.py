"""Per-trial isolated workspaces.

The source repository is never modified: plain directories are copied, git
sources are cloned (`--no-hardlinks`) and checked out detached at the pinned
ref. Plain-directory sources get a deterministic starting commit (fixed
author/committer/date), so the same fixture content always yields the same
SHA, which run metadata records as `starting_commit`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from qwenbench.bench.suite import Case

IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "*.pyc", ".coverage")
FIXED_GIT_ENV = {
    "GIT_AUTHOR_NAME": "qwenbench", "GIT_AUTHOR_EMAIL": "qwenbench@localhost",
    "GIT_COMMITTER_NAME": "qwenbench", "GIT_COMMITTER_EMAIL": "qwenbench@localhost",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **(env or {})})
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {p.stderr.strip()}")
    return p.stdout.strip()


@dataclass
class Workspace:
    root: Path          # temp dir holding repo/ and scratch/
    repo: Path
    scratch: Path
    starting_commit: str
    source_desc: str

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def create(case: Case, parent: Path | None = None) -> Workspace:
    root = Path(tempfile.mkdtemp(prefix=f"qwenbench-{case.id}-", dir=parent))
    repo = root / "repo"
    scratch = root / "scratch"
    scratch.mkdir()
    try:
        if case.source.git:
            src = case.source.git
            local = case.resolve(src)
            if local and local.exists():
                src = str(local)
            subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", src, str(repo)], check=True,
                           capture_output=True)
            _git(repo, "checkout", "--quiet", "--detach", case.source.ref)
            source_desc = f"git:{case.source.git}@{case.source.ref}"
        else:
            src_dir = case.resolve(case.source.path)
            if not src_dir or not src_dir.is_dir():
                raise FileNotFoundError(f"{case.id}: source path {case.source.path} not found")
            shutil.copytree(src_dir, repo, ignore=IGNORE)
            _git(repo, "init", "--quiet", "-b", "main")
            source_desc = f"dir:{src_dir}"
        overlay = case.resolve(case.source.overlay)
        if overlay:
            shutil.copytree(overlay, repo, dirs_exist_ok=True)
        if case.source.path or overlay:
            _git(repo, "add", "-A")
            _git(repo, "commit", "--quiet", "--allow-empty", "-m", f"qwenbench starting state for {case.id}",
                 env=FIXED_GIT_ENV)
        commit = _git(repo, "rev-parse", "HEAD")
        return Workspace(root=root, repo=repo, scratch=scratch, starting_commit=commit, source_desc=source_desc)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def restore_paths(ws: Workspace, paths: list[str]) -> None:
    """Reset paths to the starting commit (deleting files the agent added under them)."""
    for p in paths:
        target = ws.repo / p
        if p.endswith("/") or target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        subprocess.run(["git", "checkout", ws.starting_commit, "--", p], cwd=ws.repo, capture_output=True)


def apply_overlay(ws: Workspace, overlay: Path) -> None:
    shutil.copytree(overlay, ws.repo, dirs_exist_ok=True)
