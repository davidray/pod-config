"""Filesystem locations: the bench repo (config, benchmarks) and local mutable state."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """The qwenbench repository root (holds config/, benchmarks/, containers/).

    QWEN_BENCH_HOME wins; otherwise walk up from this file, which works for the
    editable install that `make setup` performs.
    """
    env = os.environ.get("QWEN_BENCH_HOME")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "models.yaml").exists():
            return parent
    raise RuntimeError(
        "Cannot locate the qwenbench repository. Set QWEN_BENCH_HOME to the repo checkout."
    )


def config_dir() -> Path:
    return repo_root() / "config"


def state_dir() -> Path:
    """Mutable local state: live pod sessions, dispatch jobs, overrides.

    Deliberately outside any project checkout so an agent working in a project
    cannot tamper with it via relative paths.
    """
    env = os.environ.get("QWEN_STATE_DIR")
    base = Path(env).expanduser() if env else Path.home() / ".local" / "state" / "qwenbench"
    base.mkdir(parents=True, exist_ok=True)
    return base


def results_dir() -> Path:
    env = os.environ.get("QWEN_RESULTS_DIR")
    path = Path(env).expanduser() if env else repo_root() / "results"
    path.mkdir(parents=True, exist_ok=True)
    return path
