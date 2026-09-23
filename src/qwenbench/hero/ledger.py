"""Per-project routing evidence: `<project>/.qwen-routing/`.

    config.json      binding written by `qwen hero configure` (enforce flag, bench home)
    decisions.jsonl  every hook decision (allow/deny, agent, route, reason)
    ledger.jsonl     every dispatch (request, route, served model, pod, files + blobs)
    baseline.json    working-tree snapshot taken at Claude session start
    dispatches/<id>/ transcript, tool log, diff, requests for each dispatch

This is how you prove after the fact which model performed each role.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from qwenbench.secrets import redact

DIR = ".qwen-routing"


def routing_dir(project: Path) -> Path:
    return Path(project) / DIR


def load_binding(project: Path) -> dict[str, Any] | None:
    p = routing_dir(project) / "config.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return {"enforce": True, "corrupt": True}  # fail closed


def enforcement_active(project: Path) -> bool:
    b = load_binding(project)
    return bool(b and b.get("enforce", True))


def _append(project: Path, name: str, record: dict[str, Any], /) -> dict[str, Any]:
    d = routing_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    record = redact(record)
    with (d / name).open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def log_decision(project: Path, /, **record: Any) -> dict[str, Any]:
    return _append(project, "decisions.jsonl", record)


def log_dispatch(project: Path, /, **record: Any) -> dict[str, Any]:
    return _append(project, "ledger.jsonl", record)


def read_jsonl(project: Path, name: str) -> list[dict[str, Any]]:
    p = routing_dir(project) / name
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def task_key(role: str, task: str, spec_ref: str | None) -> str:
    """Stable identity for 'the same task' to count dispatch attempts."""
    basis = f"{role}\n{spec_ref or ''}\n{' '.join(task.split())}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def attempts_for(project: Path, key: str) -> int:
    return sum(1 for r in read_jsonl(project, "ledger.jsonl") if r.get("event") == "dispatch-end" and r.get("task_key") == key)


def project_id(project: Path) -> str:
    return hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()[:16]
