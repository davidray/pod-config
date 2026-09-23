"""Local session state for live endpoints (one JSON file per profile) and an
append-only event log. Lives in the state dir (0700), never in results."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from qwenbench.paths import state_dir
from qwenbench.secrets import redact


@dataclass
class Session:
    profile: str
    compute: str               # "runpod-pods"
    pod_id: str
    pod_name: str
    endpoint_url: str          # OpenAI-compatible base, e.g. https://<pod>-8000.proxy.runpod.net/v1
    watchdog_url: str
    api_key: str               # per-session bearer token for vLLM + supervisor
    served_model_name: str
    gpu_type_id: str
    cloud: str
    data_center_id: str | None
    network_volume_id: str | None
    requested_at: float
    ready_at: float | None = None
    cost_per_hr: float | None = None
    cost_source: str = "list-price"
    limits: dict[str, Any] = field(default_factory=dict)
    startup: dict[str, Any] = field(default_factory=dict)
    vllm_argv: list[str] = field(default_factory=list)
    config_fingerprint: str = ""

    def public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("api_key")
        return d


def _sessions_dir() -> Path:
    d = state_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def session_path(profile: str) -> Path:
    return _sessions_dir() / f"{profile}.json"


def save_session(s: Session) -> None:
    path = session_path(s.profile)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_session(profile: str) -> Session | None:
    path = session_path(profile)
    if not path.exists():
        return None
    return Session(**json.loads(path.read_text()))


def clear_session(profile: str) -> None:
    session_path(profile).unlink(missing_ok=True)


def all_sessions() -> list[Session]:
    return [Session(**json.loads(p.read_text())) for p in sorted(_sessions_dir().glob("*.json"))]


def log_event(kind: str, **data: Any) -> dict[str, Any]:
    """Append to state/events.jsonl: up/down/auto-shutdown/override/etc."""
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": kind, **data}
    record = redact(record)
    with (state_dir() / "events.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def read_events(limit: int = 50, kinds: set[str] | None = None) -> list[dict[str, Any]]:
    path = state_dir() / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if kinds:
        rows = [r for r in rows if r.get("event") in kinds]
    return rows[-limit:]
