"""Attribution audit: which model changed which files.

A changed implementation file is *attributed to Qwen* when its current content
(blob SHA) equals the content recorded in the result tree of a Qwen dispatch
logged in .qwen-routing/ledger.jsonl. Anything else that changed since the
session baseline (outside frontier-writable spec/doc paths) is flagged as
unattributed: either Claude edited it despite the hooks (e.g. via a shell
trick the heuristics missed) or a human did.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from qwenbench.agent import snapshot
from qwenbench.config import RolePolicyFile
from qwenbench.hero.hooks import _matches
from qwenbench.hero.ledger import read_jsonl, routing_dir


def baseline_tree(project: Path) -> str | None:
    p = routing_dir(project) / "baseline.json"
    if p.exists():
        return json.loads(p.read_text()).get("tree")
    head = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=project, capture_output=True, text=True)
    return head.stdout.strip() or None


def unattributed_changes(project: Path, policy: RolePolicyFile) -> list[str]:
    base = baseline_tree(project)
    if not base:
        return []
    now = snapshot.snapshot(project)
    if now == base:
        return []
    enf = policy.enforcement
    qwen_blobs: dict[str, set[str | None]] = {}
    for rec in read_jsonl(project, "ledger.jsonl"):
        if rec.get("event") != "dispatch-end":
            continue
        for f in rec.get("files") or []:
            qwen_blobs.setdefault(f["path"], set()).add(f.get("blob"))
    flagged = []
    for change in snapshot.changes(project, base, now):
        if _matches(change.path, enf.frontier_writable) or change.path.startswith(".qwen-routing/"):
            continue
        blob = snapshot.file_blob(project, now, change.path)
        if blob not in qwen_blobs.get(change.path, set()):
            flagged.append(change.path)
    return flagged


def audit_report(project: Path, policy: RolePolicyFile) -> dict[str, Any]:
    decisions = read_jsonl(project, "decisions.jsonl")
    ledger = read_jsonl(project, "ledger.jsonl")
    ends = [r for r in ledger if r.get("event") == "dispatch-end"]
    denied = [d for d in decisions if d.get("event") == "pre-tool-use" and not d.get("allow")]
    return {
        "dispatches": [{
            "ts": r["ts"], "dispatch_id": r.get("dispatch_id"), "role": r.get("role"), "status": r.get("status"),
            "model": (r.get("model") or {}).get("served_models_observed"), "profile": (r.get("model") or {}).get("profile"),
            "gpu": (r.get("model") or {}).get("gpu_type_id"), "pod": (r.get("model") or {}).get("pod_id"),
            "files": [f["path"] for f in r.get("files") or []],
        } for r in ends],
        "frontier_subagents": Counter(d.get("subagent") for d in decisions
                                      if d.get("rule") == "agent-frontier"),
        "denied": Counter(d.get("rule") for d in denied),
        "denied_examples": denied[-10:],
        "overrides": [d for d in decisions if str(d.get("event", "")).startswith("override")
                      or d.get("rule") in ("agent-override", "edit-override")],
        "unattributed_changes": unattributed_changes(project, policy),
    }
