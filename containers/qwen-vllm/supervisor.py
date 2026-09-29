#!/usr/bin/env python3
"""qwenbench in-pod supervisor + watchdog.

Runs inside the pinned vLLM image (stdlib only). Responsibilities:

  1. Launch `vllm serve` with the exact argv rendered by the qwenbench CLI
     (env QWENBENCH_VLLM_ARGV, a JSON list) and tee its output to the
     persistent volume and to stdout (so the Runpod logs API sees it).
  2. Report boot phases so `qwenbench up` can distinguish
     container_started -> vllm_starting -> model_loading -> server_up.
     (The CLI itself confirms "ready" with a real inference request.)
  3. Enforce lifecycle guards and terminate THIS pod via the Runpod API:
       - idle_timeout   : no completed/running inference for N seconds
       - startup_timeout: model not serving within N seconds of boot
       - max_session    : hard cap on pod lifetime
       - max_spend      : cost_per_hr x uptime exceeds a USD cap
       - vllm_crashed   : vLLM exited; keep status up for a grace period so
                          the CLI can collect logs, then terminate
  4. Record every automatic shutdown (with reason) to
     /workspace/qwenbench/shutdowns.jsonl so it survives the pod.

HTTP status endpoint (port QWENBENCH_WATCHDOG_PORT, bearer-token protected):
  GET /status  -> JSON phase, idle clock, limits, recent shutdown history
  GET /logs    -> last N lines of the vLLM log (?n=200)
  POST /touch  -> reset the idle clock (used by `qwenbench keepalive`)
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV = os.environ
POD_ID = ENV.get("RUNPOD_POD_ID", "local")
API_KEY = ENV.get("VLLM_API_KEY", "")  # shared with vLLM; guards /status too
# A dedicated restricted key (QWENBENCH_SELF_STOP_KEY) is preferred: Runpod's
# injected pod-scoped RUNPOD_API_KEY returned 403 on its own pod (2026-09-23).
RUNPOD_KEY = ENV.get("QWENBENCH_SELF_STOP_KEY") or ENV.get("RUNPOD_API_KEY", "")
KEY_SOURCE = "self-stop-key" if ENV.get("QWENBENCH_SELF_STOP_KEY") else "pod-scoped-key"
PORT = int(ENV.get("QWENBENCH_VLLM_PORT", "8000"))
WD_PORT = int(ENV.get("QWENBENCH_WATCHDOG_PORT", "8001"))
STATE_ROOT = ENV.get("QWENBENCH_STATE_ROOT", "/workspace/qwenbench")
LOG_DIR = os.path.join(STATE_ROOT, "logs")
SHUTDOWN_LOG = os.path.join(STATE_ROOT, "shutdowns.jsonl")
POLL_S = float(ENV.get("QWENBENCH_POLL_S", "20"))
CRASH_GRACE_S = float(ENV.get("QWENBENCH_CRASH_GRACE_S", "300"))
DRY_RUN = ENV.get("QWENBENCH_DRY_RUN") == "1"  # tests: never call Runpod


def _num(name: str) -> float | None:
    raw = ENV.get(name, "").strip()
    if not raw or raw.lower() in {"off", "none", "0"}:
        return None
    return float(raw)


IDLE_TIMEOUT_S = _num("QWENBENCH_IDLE_TIMEOUT_S")
STARTUP_TIMEOUT_S = _num("QWENBENCH_STARTUP_TIMEOUT_S")
MAX_SESSION_S = _num("QWENBENCH_MAX_SESSION_S")
MAX_SPEND_USD = _num("QWENBENCH_MAX_SPEND_USD")
LIST_COST_PER_HR = _num("QWENBENCH_LIST_COST_PER_HR") or 0.0

LOADING_PATTERNS = [re.compile(p, re.I) for p in (
    r"Loading safetensors", r"Loading weights", r"Loading model", r"model weights took",
    r"Downloading", r"Starting to load model",
)]
SERVER_UP_PATTERNS = [re.compile(p, re.I) for p in (
    r"Application startup complete", r"Uvicorn running on", r"Starting vLLM API server",
)]


def now() -> float:
    return time.time()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.boot_ts = now()
        self.phase = "container_started"
        self.phase_ts: dict[str, float] = {"container_started": self.boot_ts}
        self.ready_ts: float | None = None
        self.last_activity_ts = self.boot_ts
        self.last_counters: tuple[float, float, float] | None = None
        self.requests_seen = 0.0
        self.cost_per_hr = LIST_COST_PER_HR
        self.cost_source = "list-price"
        self.api_auth_ok: bool | None = None
        self.api_probe_status: int | None = None
        self.shutdown: dict | None = None
        self.vllm_exit: int | None = None
        self.log_tail: deque[str] = deque(maxlen=2000)

    def set_phase(self, phase: str) -> None:
        with self.lock:
            if self.phase != phase:
                self.phase = phase
                self.phase_ts.setdefault(phase, now())
                log(f"phase -> {phase}")

    def snapshot(self) -> dict:
        with self.lock:
            t = now()
            uptime = t - self.boot_ts
            idle_for = t - self.last_activity_ts if self.ready_ts else None
            return {
                "pod_id": POD_ID,
                "phase": self.phase,
                "phase_times": {k: iso(v) for k, v in self.phase_ts.items()},
                "boot_time": iso(self.boot_ts),
                "uptime_s": round(uptime, 1),
                "ready_time": iso(self.ready_ts),
                "idle_for_s": round(idle_for, 1) if idle_for is not None else None,
                "requests_seen": self.requests_seen,
                "limits": {
                    "idle_timeout_s": IDLE_TIMEOUT_S,
                    "startup_timeout_s": STARTUP_TIMEOUT_S,
                    "max_session_s": MAX_SESSION_S,
                    "max_spend_usd": MAX_SPEND_USD,
                },
                "cost_per_hr": self.cost_per_hr,
                "cost_source": self.cost_source,
                "estimated_spend_usd": round(self.cost_per_hr * uptime / 3600, 4),
                "runpod_api_auth_ok": self.api_auth_ok,
                "runpod_api_key_source": KEY_SOURCE,
                "runpod_api_probe_status": self.api_probe_status,
                "vllm_exit_code": self.vllm_exit,
                "shutdown": self.shutdown,
                "history": read_history(),
            }


STATE = State()


def log(msg: str) -> None:
    print(f"[qwenbench-supervisor {iso(now())}] {msg}", flush=True)


def read_history(limit: int = 10) -> list[dict]:
    try:
        with open(SHUTDOWN_LOG) as fh:
            lines = fh.readlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError):
        return []


# ------------------------------------------------------------------ Runpod API


def runpod_call(method: str, url: str, body: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {RUNPOD_KEY}", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def probe_api() -> None:
    """Read our own pod record: learns the billed $/hr and whether the key can read.

    A failed read does not prove the key cannot stop the pod (observed
    2026-09-23: reads failed, yet self-stop worked), so this is only a hint.
    """
    if DRY_RUN or not RUNPOD_KEY:
        STATE.api_auth_ok = False if not RUNPOD_KEY else None
        return
    status, body = 0, None
    for _ in range(6):  # networking can lag container start
        status, body = runpod_call("GET", f"https://api.runpod.io/v2/pods/{POD_ID}")
        if status:
            break
        time.sleep(10)
    STATE.api_probe_status = status
    STATE.api_auth_ok = status == 200
    if status == 200 and body and body.get("cost"):
        STATE.cost_per_hr = float(body["cost"])
        STATE.cost_source = "runpod-pod-cost"
    log(f"runpod api probe ({KEY_SOURCE}): status={status} cost_per_hr={STATE.cost_per_hr} ({STATE.cost_source})")


def terminate_self(reason: str, detail: str) -> None:
    with STATE.lock:
        if STATE.shutdown:
            return
        STATE.shutdown = {"reason": reason, "detail": detail, "time": iso(now())}
    snap = STATE.snapshot()
    record = {
        "time": iso(now()), "pod_id": POD_ID, "reason": reason, "detail": detail,
        "uptime_s": snap["uptime_s"], "estimated_spend_usd": snap["estimated_spend_usd"],
        "requests_seen": snap["requests_seen"],
    }
    try:
        os.makedirs(STATE_ROOT, exist_ok=True)
        with open(SHUTDOWN_LOG, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as e:
        log(f"could not persist shutdown record: {e}")
    log(f"AUTO-SHUTDOWN reason={reason} detail={detail}")
    if DRY_RUN:
        return
    # Try the most complete release first; each is idempotent.
    attempts = [
        ("POST", f"https://api.runpod.io/v2/pods/{POD_ID}/action", {"action": "terminate"}),
        ("DELETE", f"https://api.runpod.io/v2/pods/{POD_ID}", None),
        ("POST", f"https://api.runpod.io/v2/pods/{POD_ID}/action", {"action": "stop"}),
        ("POST", f"https://rest.runpod.io/v1/pods/{POD_ID}/stop", None),
    ]
    for method, url, body in attempts:
        status, _ = runpod_call(method, url, body)
        log(f"  {method} {url} -> {status}")
        if status in (200, 202, 204):
            return
    for tool in (["runpodctl", "pod", "stop", POD_ID], ["runpodctl", "stop", "pod", POD_ID]):
        try:
            if subprocess.run(tool, timeout=60).returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    log("ERROR: could not stop this pod via the API. The local `qwenbench guard` backstop must stop it.")


# ------------------------------------------------------------------ vLLM


def http_get(path: str, timeout: float = 5) -> tuple[int, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 headers={"Authorization": f"Bearer {API_KEY}"} if API_KEY else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, ""


METRIC_RE = re.compile(r"^(vllm:[a-z_]+)(\{[^}]*\})?\s+([0-9.eE+-]+)$")


def parse_metrics(text: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for line in text.splitlines():
        m = METRIC_RE.match(line.strip())
        if m:
            totals[m.group(1)] = totals.get(m.group(1), 0.0) + float(m.group(3))
    return totals


def activity_counters(metrics: dict[str, float]) -> tuple[float, float, float]:
    done = metrics.get("vllm:request_success_total", 0.0)
    running = metrics.get("vllm:num_requests_running", 0.0)
    waiting = metrics.get("vllm:num_requests_waiting", 0.0)
    return done, running, waiting


def pump_output(proc: subprocess.Popen, logfile) -> None:
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        logfile.write(line + "\n")
        logfile.flush()
        STATE.log_tail.append(line)
        if STATE.phase in ("vllm_starting",) and any(p.search(line) for p in LOADING_PATTERNS):
            STATE.set_phase("model_loading")
        if STATE.phase in ("vllm_starting", "model_loading") and any(p.search(line) for p in SERVER_UP_PATTERNS):
            STATE.set_phase("server_starting")


def start_vllm() -> subprocess.Popen:
    argv = json.loads(ENV["QWENBENCH_VLLM_ARGV"])
    os.makedirs(LOG_DIR, exist_ok=True)
    logfile = open(os.path.join(LOG_DIR, f"vllm-{POD_ID}-{int(STATE.boot_ts)}.log"), "a")  # noqa: SIM115
    log("exec: " + " ".join(argv))
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    STATE.set_phase("vllm_starting")
    threading.Thread(target=pump_output, args=(proc, logfile), daemon=True).start()
    return proc


# ------------------------------------------------------------------ watchdog loop


def check_limits() -> None:
    snap = STATE.snapshot()
    uptime = snap["uptime_s"]
    if STATE.ready_ts is None and STARTUP_TIMEOUT_S and uptime > STARTUP_TIMEOUT_S:
        terminate_self("startup_timeout", f"model not serving after {int(uptime)}s (limit {int(STARTUP_TIMEOUT_S)}s)")
    elif MAX_SESSION_S and uptime > MAX_SESSION_S:
        terminate_self("max_session", f"pod up {int(uptime)}s (limit {int(MAX_SESSION_S)}s)")
    elif MAX_SPEND_USD and snap["estimated_spend_usd"] >= MAX_SPEND_USD:
        terminate_self("max_spend", f"estimated ${snap['estimated_spend_usd']:.2f} >= cap ${MAX_SPEND_USD:.2f}")
    elif IDLE_TIMEOUT_S and snap["idle_for_s"] is not None and snap["idle_for_s"] > IDLE_TIMEOUT_S:
        terminate_self("idle_timeout", f"no inference for {int(snap['idle_for_s'])}s (limit {int(IDLE_TIMEOUT_S)}s)")


def watchdog_tick(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is not None and STATE.vllm_exit is None:
        STATE.vllm_exit = proc.returncode
        STATE.set_phase("vllm_exited")
        log(f"vLLM exited with code {proc.returncode}; terminating in {int(CRASH_GRACE_S)}s")
        threading.Timer(CRASH_GRACE_S, terminate_self,
                        args=("vllm_crashed", f"vLLM exited with code {proc.returncode}")).start()
        return
    if STATE.vllm_exit is None:
        status, _ = http_get("/health")
        if status == 200:
            code, body = http_get("/v1/models")
            if code == 200 and STATE.ready_ts is None:
                STATE.ready_ts = now()
                STATE.last_activity_ts = STATE.ready_ts
                STATE.set_phase("server_up")
            if STATE.ready_ts is not None:
                _, text = http_get("/metrics")
                counters = activity_counters(parse_metrics(text))
                with STATE.lock:
                    done, running, waiting = counters
                    STATE.requests_seen = done
                    if running > 0 or waiting > 0 or (STATE.last_counters and done != STATE.last_counters[0]):
                        STATE.last_activity_ts = now()
                    STATE.last_counters = counters
    check_limits()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # quiet
        pass

    def _authorized(self) -> bool:
        return not API_KEY or self.headers.get("Authorization") == f"Bearer {API_KEY}"

    def _send(self, code: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path.startswith("/status"):
            return self._send(200, STATE.snapshot())
        if self.path.startswith("/logs"):
            n = 200
            m = re.search(r"[?&]n=(\d+)", self.path)
            if m:
                n = min(int(m.group(1)), 2000)
            return self._send(200, {"lines": list(STATE.log_tail)[-n:]})
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path.startswith("/touch"):
            with STATE.lock:
                STATE.last_activity_ts = now()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def main() -> None:
    log(f"boot pod={POD_ID} idle={IDLE_TIMEOUT_S} startup={STARTUP_TIMEOUT_S} "
        f"max_session={MAX_SESSION_S} max_spend={MAX_SPEND_USD}")
    server = ThreadingHTTPServer(("0.0.0.0", WD_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=probe_api, daemon=True).start()
    proc = start_vllm() if ENV.get("QWENBENCH_VLLM_ARGV") else None

    def forward(sig, _frame):
        if proc and proc.poll() is None:
            proc.send_signal(sig)
        sys.exit(0)

    signal.signal(signal.SIGTERM, forward)
    while True:
        try:
            watchdog_tick(proc)
        except Exception as e:  # never let the watchdog die
            log(f"watchdog error: {e!r}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
