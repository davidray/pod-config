"""Attribution audit: which model changed which files.

Every changed implementation file since the baseline gets one of:

  qwen             its current content (blob SHA) equals the result of a Qwen dispatch in ledger.jsonl
  claude-override  Claude edited it through a tool call allowed by a human override
  unattributed     anything else: Claude got around the hooks, the hooks were not running, or a human edited it

The baseline is the working tree the first time a Claude session in this
project hit a hook (SessionStart, or the first PreToolUse if the session
started elsewhere and moved here). Without one, the audit falls back to HEAD
and says so: committed changes are then invisible to it.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from qwenbench.agent import snapshot
from qwenbench.config import RolePolicyFile
from qwenbench.hero.hooks import _matches
from qwenbench.hero.ledger import read_jsonl, routing_dir

# `qwenbench hero verify` sends one real hook event under this id; it is not a Claude session.
VERIFY_SESSION = "qwen-hero-verify"
QWEN, OVERRIDE, UNATTRIBUTED = "qwen", "claude-override", "unattributed"


def _baselines(project: Path) -> Path:
    return routing_dir(project) / "baselines"


def record_baseline(project: Path, session_id: str | None) -> None:
    """Snapshot the working tree the first time a session touches this project (once per session)."""
    if not session_id or session_id == VERIFY_SESSION:
        return
    path = _baselines(project) / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id)}.json"
    if path.exists():
        return
    tree = snapshot.snapshot(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tree": tree, "session_id": session_id, "ts": time.time()}))


def baseline(project: Path, session_id: str | None = None) -> tuple[str | None, str]:
    """(tree, description). A given session's baseline, else the earliest recorded, else HEAD."""
    recorded = sorted((json.loads(p.read_text()) for p in _baselines(project).glob("*.json")),
                      key=lambda b: b.get("ts", 0))
    if session_id:
        recorded = [b for b in recorded if b.get("session_id") == session_id] or recorded
    if recorded:
        return recorded[0]["tree"], f"session {recorded[0]['session_id']} baseline"
    head = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=project, capture_output=True, text=True)
    return (head.stdout.strip() or None,
            "HEAD: no Claude session baseline was recorded, so committed changes are invisible to this audit")


def attribution(project: Path, policy: RolePolicyFile, session_id: str | None = None) -> dict[str, str]:
    base, _ = baseline(project, session_id)
    if not base:
        return {}
    now = snapshot.snapshot(project)
    if now == base:
        return {}
    qwen_blobs: dict[str, set[str | None]] = {}
    for rec in read_jsonl(project, "ledger.jsonl"):
        if rec.get("event") == "dispatch-end":
            for f in rec.get("files") or []:
                qwen_blobs.setdefault(f["path"], set()).add(f.get("blob"))
    override_targets = {_rel(project, d["target"]) for d in read_jsonl(project, "decisions.jsonl")
                        if d.get("allow") and d.get("rule") == "edit-override" and d.get("target")}
    enf = policy.enforcement
    out: dict[str, str] = {}
    for change in snapshot.changes(project, base, now):
        if _matches(change.path, enf.frontier_writable) or change.path.startswith(".qwen-routing/"):
            continue
        if snapshot.file_blob(project, now, change.path) in qwen_blobs.get(change.path, set()):
            out[change.path] = QWEN
        elif change.path in override_targets:
            out[change.path] = OVERRIDE
        else:
            out[change.path] = UNATTRIBUTED
    return out


def unattributed_changes(project: Path, policy: RolePolicyFile, session_id: str | None = None) -> list[str]:
    return [p for p, who in attribution(project, policy, session_id).items() if who == UNATTRIBUTED]


def _rel(project: Path, target: str) -> str:
    p = Path(target)
    try:
        return str(p.resolve().relative_to(project.resolve())) if p.is_absolute() else str(p)
    except ValueError:
        return target


def audit_report(project: Path, policy: RolePolicyFile) -> dict[str, Any]:
    decisions = [d for d in read_jsonl(project, "decisions.jsonl") if d.get("session_id") != VERIFY_SESSION]
    ledger = read_jsonl(project, "ledger.jsonl")
    ends = [r for r in ledger if r.get("event") == "dispatch-end"]
    denied = [d for d in decisions if d.get("event") == "pre-tool-use" and not d.get("allow")]
    sessions = sorted({d["session_id"] for d in decisions
                       if d.get("session_id") and d.get("event") in ("pre-tool-use", "session-start")})
    base, base_desc = baseline(project)
    return {
        "dispatches": [{
            "ts": r["ts"], "dispatch_id": r.get("dispatch_id"), "role": r.get("role"), "status": r.get("status"),
            "model": (r.get("model") or {}).get("served_models_observed"), "profile": (r.get("model") or {}).get("profile"),
            "gpu": (r.get("model") or {}).get("gpu_type_id"), "pod": (r.get("model") or {}).get("pod_id"),
            "files": [f["path"] for f in r.get("files") or []], "warnings": r.get("warnings") or [],
        } for r in ends],
        # Hook events from real Claude sessions. None means the hooks never ran in a session here,
        # so nothing was enforced, whatever the dispatch ledger says.
        "hooked_sessions": sessions,
        "baseline": base_desc,
        "frontier_subagents": Counter(d.get("subagent") for d in decisions if d.get("rule") == "agent-frontier"),
        "denied": Counter(d.get("rule") for d in denied),
        "denied_examples": denied[-10:],
        "overrides": [d for d in decisions if str(d.get("event", "")).startswith("override")
                      or d.get("rule") in ("agent-override", "edit-override")],
        "attribution": attribution(project, policy),
        "unattributed_changes": unattributed_changes(project, policy),
    }
