"""Local backstop for automatic shutdown (`qwenbench guard <profile>`).

The in-pod supervisor is the primary idle/session/spend enforcer: it keeps
working when this laptop sleeps. It terminates the pod with Runpod's
pod-scoped API key, whose permissions Runpod does not document. This guard
runs detached on the workstation with the full account key and terminates
the pod when:

  - the supervisor declared a shutdown but the pod is still billing
    `stuck_grace_s` later (the pod-scoped key could not stop it)
  - the supervisor reports idle beyond idle_timeout + grace
  - session age exceeds max_session + grace
  - estimated spend exceeds max_spend
  - the supervisor has been unreachable for `unreachable_s` on a RUNNING pod
    that was previously ready (it cannot enforce anything we can't see)

`decide()` is pure so it can be unit-tested without a network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from qwenbench.paths import state_dir

GRACE_S = 300


@dataclass
class GuardInputs:
    now: float
    pod_live: bool
    session_started: float
    ready: bool
    limits: dict[str, Any]
    supervisor: dict[str, Any] | None
    supervisor_last_seen: float | None
    cost_per_hr: float | None
    shutdown_seen_at: float | None
    unreachable_s: float = 900


def decide(i: GuardInputs) -> tuple[str, str] | None:
    """Return (reason, detail) if the guard must terminate the pod, else None."""
    if not i.pod_live:
        return None
    age = i.now - i.session_started
    idle_limit = i.limits.get("idle_timeout_s")
    max_session = i.limits.get("max_session_s")
    max_spend = i.limits.get("max_spend_usd")
    sup = i.supervisor

    if i.shutdown_seen_at and i.now - i.shutdown_seen_at > GRACE_S:
        return ("supervisor_shutdown_stuck",
                "in-pod supervisor declared a shutdown but the pod is still live; pod-scoped key likely lacks permission")
    if max_session and age > max_session + GRACE_S:
        return ("max_session", f"session age {int(age)}s exceeds {int(max_session)}s (+{GRACE_S}s grace)")
    if max_spend and i.cost_per_hr and (i.cost_per_hr * age / 3600) > max_spend:
        return ("max_spend", f"estimated ${i.cost_per_hr * age / 3600:.2f} exceeds ${max_spend:.2f}")
    if sup:
        idle = sup.get("idle_for_s")
        if idle_limit and idle is not None and idle > idle_limit + GRACE_S:
            return ("idle_timeout", f"supervisor reports idle {int(idle)}s (limit {int(idle_limit)}s + grace)")
    elif i.ready and i.supervisor_last_seen and i.now - i.supervisor_last_seen > i.unreachable_s:
        return ("supervisor_unreachable",
                f"no supervisor heartbeat for {int(i.now - i.supervisor_last_seen)}s; cannot verify idleness")
    return None


def guard_pid_path(profile: str):
    return state_dir() / "guards" / f"{profile}.pid"


def guard_running(profile: str) -> int | None:
    path = guard_pid_path(profile)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def spawn_guard(profile: str) -> int:
    """Start `qwenbench guard <profile>` detached from this terminal."""
    existing = guard_running(profile)
    if existing:
        return existing
    log_dir = state_dir() / "guards"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / f"{profile}.log").open("a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "qwenbench.cli.main", "guard", profile],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    guard_pid_path(profile).write_text(str(proc.pid))
    return proc.pid


def run_guard(provider, profile, poll_s: float = 60, clock=time.time, sleep=time.sleep) -> str:
    """Loop until the pod is gone. Returns the exit reason."""
    from qwenbench.state import load_session, log_event

    guard_pid_path(profile.name).parent.mkdir(parents=True, exist_ok=True)
    guard_pid_path(profile.name).write_text(str(os.getpid()))
    last_seen: float | None = None
    shutdown_seen: float | None = None
    try:
        while True:
            session = load_session(profile.name)
            if not session:
                return "no-session"
            pod = provider.client.get_pod(session.pod_id)
            live = bool(pod and pod.is_live)
            if not live:
                return "pod-gone"
            sup = provider.supervisor_status(session.watchdog_url, session.api_key)
            now = clock()
            if sup:
                last_seen = now
                if sup.get("shutdown") and not shutdown_seen:
                    shutdown_seen = now
                    log_event("auto-shutdown", profile=profile.name, pod_id=session.pod_id,
                              source="in-pod-supervisor", **sup["shutdown"])
            verdict = decide(GuardInputs(
                now=now, pod_live=live, session_started=session.requested_at, ready=bool(session.ready_at),
                limits=session.limits, supervisor=sup, supervisor_last_seen=last_seen,
                cost_per_hr=session.cost_per_hr, shutdown_seen_at=shutdown_seen,
            ))
            if verdict:
                reason, detail = verdict
                log_event("auto-shutdown", profile=profile.name, pod_id=session.pod_id,
                          source="local-guard", reason=reason, detail=detail)
                provider.down(profile, reason=f"guard: {reason}: {detail}")
                return reason
            sleep(poll_s)
    finally:
        guard_pid_path(profile.name).unlink(missing_ok=True)
