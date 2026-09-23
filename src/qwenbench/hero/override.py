"""Explicit human override: let Claude perform a Qwen-assigned role.

Overrides are stored in the qwenbench state dir (outside the project, so an
agent in the project cannot write them), expire, and can only be created from
an interactive terminal: `qwenbench override grant` refuses when stdin is not a TTY,
and the PreToolUse hook denies Claude any `qwenbench override` command. Every grant,
use and revocation is logged to the project ledger and the global event log.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from qwenbench.hero.ledger import log_decision, project_id
from qwenbench.paths import state_dir
from qwenbench.state import log_event

SCOPES = ("agent", "edits")  # spawn the role on Claude / let Claude edit implementation files


@dataclass
class Override:
    project: str
    role: str            # agent name, or "*" for all Qwen-routed roles
    scope: str
    reason: str
    granted_at: float
    expires_at: float
    granted_by: str

    def active(self, now: float | None = None) -> bool:
        return (now or time.time()) < self.expires_at


def _path(project: Path) -> Path:
    d = state_dir() / "overrides"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{project_id(project)}.json"


def load(project: Path) -> list[Override]:
    p = _path(project)
    if not p.exists():
        return []
    return [Override(**o) for o in json.loads(p.read_text())]


def _save(project: Path, items: list[Override]) -> None:
    _path(project).write_text(json.dumps([asdict(o) for o in items], indent=2))


def grant(project: Path, role: str, scope: str, reason: str, ttl_s: float, granted_by: str) -> Override:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if not reason.strip():
        raise ValueError("an override needs a reason")
    now = time.time()
    o = Override(str(Path(project).resolve()), role, scope, reason.strip(), now, now + ttl_s, granted_by)
    items = [x for x in load(project) if x.active(now)] + [o]
    _save(project, items)
    log_decision(project, event="override-granted", **asdict(o))
    log_event("override-granted", **asdict(o))
    return o


def revoke(project: Path, role: str | None = None) -> int:
    items = load(project)
    keep = [o for o in items if role is not None and o.role != role]
    _save(project, keep)
    removed = len(items) - len(keep)
    log_decision(project, event="override-revoked", role=role or "*", removed=removed)
    return removed


def find_active(project: Path, role: str, scope: str) -> Override | None:
    now = time.time()
    for o in load(project):
        if o.active(now) and o.scope == scope and o.role in (role, "*"):
            return o
    return None
