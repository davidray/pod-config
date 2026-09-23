"""Endpoint readiness as an explicit finite state machine.

`qwen up` feeds observations (Runpod pod status, in-pod supervisor status, and
the result of a real inference probe) into `ReadinessMachine.observe()`. The
machine never reports READY because Runpod says RUNNING: READY requires a
successful authenticated inference request against the expected model.

    REQUESTED -> ALLOCATED -> CONTAINER_STARTED -> VLLM_STARTED
              -> MODEL_LOADED -> READY
    any non-terminal -> FAILED | TIMED_OUT

Phases may be observed out of order (e.g. a poll gap skips VLLM_STARTED); a
skipped phase is stamped at the time the later phase was seen and flagged as
`inferred` so startup timings stay honest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum


class Phase(IntEnum):
    REQUESTED = 0
    ALLOCATED = 1           # Runpod assigned a machine (pod exists, PROVISIONING/STARTING)
    CONTAINER_STARTED = 2   # our supervisor answers on the watchdog port
    VLLM_STARTED = 3        # vLLM process spawned
    MODEL_LOADED = 4        # vLLM /health + /v1/models answer
    READY = 5               # a real chat completion against the served model succeeded
    FAILED = 90
    TIMED_OUT = 91

    @property
    def terminal(self) -> bool:
        return self in (Phase.READY, Phase.FAILED, Phase.TIMED_OUT)


LABELS = {
    Phase.REQUESTED: "pod requested",
    Phase.ALLOCATED: "pod allocated",
    Phase.CONTAINER_STARTED: "container started",
    Phase.VLLM_STARTED: "vLLM process started",
    Phase.MODEL_LOADED: "model loaded",
    Phase.READY: "endpoint ready (inference verified)",
    Phase.FAILED: "failed",
    Phase.TIMED_OUT: "timed out",
}

SUPERVISOR_PHASES = {
    "container_started": Phase.CONTAINER_STARTED,
    "vllm_starting": Phase.VLLM_STARTED,
    "model_loading": Phase.VLLM_STARTED,
    "server_starting": Phase.VLLM_STARTED,
    "server_up": Phase.MODEL_LOADED,
}


@dataclass
class Observation:
    pod_status: str | None = None           # Runpod v2 status
    supervisor_phase: str | None = None     # from GET :8001/status, None if unreachable
    health_ok: bool = False                 # vLLM /health 200 and /v1/models lists the served model
    probe_ok: bool = False                  # real completion succeeded with the expected model id
    probe_error: str | None = None
    detail: str | None = None


@dataclass
class Transition:
    phase: Phase
    at: float
    inferred: bool = False
    detail: str | None = None


@dataclass
class ReadinessMachine:
    startup_timeout_s: float
    clock: callable = time.time  # type: ignore[valid-type]
    phase: Phase = Phase.REQUESTED
    started_at: float = 0.0
    history: list[Transition] = field(default_factory=list)
    failure: str | None = None

    def __post_init__(self) -> None:
        self.started_at = self.clock()
        self.history.append(Transition(Phase.REQUESTED, self.started_at))

    # -------------------------------------------------------------- transitions

    def _advance_to(self, target: Phase, detail: str | None = None) -> None:
        if self.phase.terminal or target <= self.phase:
            return
        now = self.clock()
        for p in Phase:
            if self.phase < p < target and p < Phase.FAILED:
                self.history.append(Transition(p, now, inferred=True))
        self.history.append(Transition(target, now, detail=detail))
        self.phase = target

    def _fail(self, phase: Phase, reason: str) -> None:
        if self.phase.terminal:
            return
        self.failure = reason
        self.history.append(Transition(phase, self.clock(), detail=reason))
        self.phase = phase

    def observe(self, obs: Observation) -> Phase:
        if self.phase.terminal:
            return self.phase
        if obs.pod_status in ("ERROR", "TERMINATED", "EXITED"):
            self._fail(Phase.FAILED, f"pod entered {obs.pod_status}" + (f": {obs.detail}" if obs.detail else ""))
            return self.phase
        if obs.supervisor_phase == "vllm_exited":
            self._fail(Phase.FAILED, "vLLM process exited during startup (see `qwen logs`)")
            return self.phase

        if obs.pod_status in ("PROVISIONING", "STARTING", "RUNNING"):
            self._advance_to(Phase.ALLOCATED)
        if obs.supervisor_phase in SUPERVISOR_PHASES:
            self._advance_to(SUPERVISOR_PHASES[obs.supervisor_phase])
        if obs.health_ok:
            self._advance_to(Phase.MODEL_LOADED)
        if obs.probe_ok:
            self._advance_to(Phase.READY)

        if not self.phase.terminal and self.elapsed() > self.startup_timeout_s:
            self._fail(Phase.TIMED_OUT, f"not ready after {int(self.elapsed())}s in phase {LABELS[self.phase]}"
                       + (f"; last probe error: {obs.probe_error}" if obs.probe_error else ""))
        return self.phase

    # -------------------------------------------------------------- reporting

    def elapsed(self) -> float:
        return self.clock() - self.started_at

    def timings(self) -> dict[str, dict]:
        """Seconds since request for each phase reached (first time only)."""
        out: dict[str, dict] = {}
        for t in self.history:
            key = t.phase.name.lower()
            if key not in out:
                out[key] = {"at": t.at, "since_request_s": round(t.at - self.started_at, 2), "inferred": t.inferred}
        return out
