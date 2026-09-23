"""Working-tree snapshots as git tree objects.

`snapshot()` stages the whole working tree (respecting .gitignore) into a
throwaway index file and writes a tree object. Nothing touches HEAD, refs, the
real index, or the files, so it is safe on a developer's live checkout. Two
snapshots give an exact diff of what a dispatch changed, including new and
deleted files.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from qwenbench.agent.protocol import FileChange


class NotAGitRepo(RuntimeError):
    pass


def _git(repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    full_env = {**os.environ, **(env or {})}
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=full_env)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def is_git_repo(repo: Path) -> bool:
    p = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo, capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "true"


def snapshot(repo: Path) -> str:
    if not is_git_repo(repo):
        raise NotAGitRepo(f"{repo} is not a git working tree")
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        # Seed with a copy of the real index so git can reuse its stat cache.
        real_index = Path(repo) / _git(repo, "rev-parse", "--git-path", "index").strip()
        if real_index.exists():
            index.write_bytes(real_index.read_bytes())
        env = {"GIT_INDEX_FILE": str(index)}
        _git(repo, "add", "-A", ".", env=env)
        return _git(repo, "write-tree", env=env).strip()


def diff(repo: Path, base: str, head: str) -> str:
    return _git(repo, "diff", "--binary", "--no-color", base, head)


def changes(repo: Path, base: str, head: str) -> list[FileChange]:
    status = {}
    for line in _git(repo, "diff", "--name-status", "-M", base, head).splitlines():
        parts = line.split("\t")
        code = parts[0][0]
        path = parts[-1]
        status[path] = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}.get(code, "modified")
    out = []
    for line in _git(repo, "diff", "--numstat", "-M", base, head).splitlines():
        added, deleted, path = line.split("\t", 2)
        if " => " in path or "{" in path:
            path = next((p for p in status if path.endswith(p.split("/")[-1])), path)
        out.append(FileChange(path=path, status=status.get(path, "modified"),
                              added=int(added) if added.isdigit() else 0,
                              deleted=int(deleted) if deleted.isdigit() else 0))
    return out


def file_blob(repo: Path, tree: str, path: str) -> str | None:
    """Blob SHA of `path` in `tree` (None if absent)."""
    out = _git(repo, "ls-tree", tree, "--", path, check=False).strip()
    return out.split()[2] if out else None
