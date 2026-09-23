"""Idle / session / spend shutdown: the in-pod supervisor and the local guard."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from qwenbench.paths import repo_root
from qwenbench.runpod.guard import GRACE_S, GuardInputs, decide

SUPERVISOR = repo_root() / "containers" / "qwen-vllm" / "supervisor.py"


def load_supervisor(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("supervisor_under_test", SUPERVISOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_metrics_parsing(monkeypatch):
    sup = load_supervisor(monkeypatch)
    text = """# HELP vllm:request_success_total ...
vllm:request_success_total{finished_reason="stop",model_name="m"} 3.0
vllm:request_success_total{finished_reason="length",model_name="m"} 2.0
vllm:num_requests_running{model_name="m"} 1.0
vllm:num_requests_waiting{model_name="m"} 0.0
"""
    m = sup.parse_metrics(text)
    assert sup.activity_counters(m) == (5.0, 1.0, 0.0)


def test_limits_trigger_with_reasons(monkeypatch, tmp_path):
    sup = load_supervisor(monkeypatch, QWENBENCH_DRY_RUN="1", QWENBENCH_STATE_ROOT=str(tmp_path),
                          QWENBENCH_IDLE_TIMEOUT_S="60", QWENBENCH_MAX_SESSION_S="off",
                          QWENBENCH_MAX_SPEND_USD="off", QWENBENCH_STARTUP_TIMEOUT_S="600")
    sup.SHUTDOWN_LOG = str(tmp_path / "shutdowns.jsonl")
    sup.STATE_ROOT = str(tmp_path)
    s = sup.STATE
    s.ready_ts = time.time() - 500
    s.last_activity_ts = time.time() - 30
    sup.check_limits()
    assert s.shutdown is None  # active 30s ago
    s.last_activity_ts = time.time() - 61
    sup.check_limits()
    assert s.shutdown["reason"] == "idle_timeout"
    rec = json.loads((tmp_path / "shutdowns.jsonl").read_text().splitlines()[-1])
    assert rec["reason"] == "idle_timeout" and "no inference" in rec["detail"]


def test_startup_timeout_and_spend(monkeypatch, tmp_path):
    sup = load_supervisor(monkeypatch, QWENBENCH_DRY_RUN="1", QWENBENCH_STATE_ROOT=str(tmp_path),
                          QWENBENCH_STARTUP_TIMEOUT_S="100", QWENBENCH_MAX_SPEND_USD="1.00",
                          QWENBENCH_LIST_COST_PER_HR="2.0", QWENBENCH_IDLE_TIMEOUT_S="off")
    sup.SHUTDOWN_LOG = str(tmp_path / "s.jsonl")
    sup.STATE.boot_ts = time.time() - 101
    sup.check_limits()
    assert sup.STATE.shutdown["reason"] == "startup_timeout"
    sup.STATE.shutdown = None
    sup.STATE.ready_ts = time.time()
    sup.STATE.boot_ts = time.time() - 1801  # 0.5h x $2/h = $1.00
    sup.check_limits()
    assert sup.STATE.shutdown["reason"] == "max_spend"


def test_idle_disabled_means_no_idle_shutdown(monkeypatch, tmp_path):
    sup = load_supervisor(monkeypatch, QWENBENCH_DRY_RUN="1", QWENBENCH_IDLE_TIMEOUT_S="off",
                          QWENBENCH_MAX_SESSION_S="off", QWENBENCH_MAX_SPEND_USD="off", QWENBENCH_STARTUP_TIMEOUT_S="off")
    sup.SHUTDOWN_LOG = str(tmp_path / "s.jsonl")
    sup.STATE.ready_ts = time.time() - 99999
    sup.STATE.last_activity_ts = time.time() - 99999
    sup.check_limits()
    assert sup.STATE.shutdown is None


class FakeVLLM:
    """/health, /v1/models and /metrics whose success counter we control."""

    def __init__(self):
        self.done = 0
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                if self.path == "/metrics":
                    body = (f'vllm:request_success_total{{finished_reason="stop"}} {me.done}\n'
                            'vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n').encode()
                elif self.path == "/v1/models":
                    body = b'{"data":[{"id":"m"}]}'
                else:
                    body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.slow
def test_supervisor_process_idles_out_and_logs_reason(tmp_path):
    """Run the real supervisor script: activity resets the clock; silence shuts down."""
    vllm = FakeVLLM()
    wd = free_port()
    env = {**os.environ, "QWENBENCH_DRY_RUN": "1", "QWENBENCH_VLLM_PORT": str(vllm.port),
           "QWENBENCH_WATCHDOG_PORT": str(wd), "QWENBENCH_STATE_ROOT": str(tmp_path), "QWENBENCH_POLL_S": "0.2",
           "QWENBENCH_IDLE_TIMEOUT_S": "2", "QWENBENCH_STARTUP_TIMEOUT_S": "30", "VLLM_API_KEY": "sekrit-token-123"}
    env.pop("QWENBENCH_VLLM_ARGV", None)
    proc = subprocess.Popen([sys.executable, str(SUPERVISOR)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        url = f"http://127.0.0.1:{wd}/status"
        headers = {"Authorization": "Bearer sekrit-token-123"}
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if httpx.get(url, headers=headers).json()["phase"] == "server_up":
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        assert httpx.get(url).status_code == 401  # status requires the session token
        for _ in range(8):  # keep it busy for ~3s: longer than the idle timeout
            vllm.done += 1
            time.sleep(0.4)
        assert httpx.get(url, headers=headers).json()["shutdown"] is None
        time.sleep(3)
        st = httpx.get(url, headers=headers).json()
        assert st["shutdown"]["reason"] == "idle_timeout"
        assert st["requests_seen"] >= 8
        assert st["history"][-1]["reason"] == "idle_timeout"  # persisted to the volume
    finally:
        proc.terminate()
        proc.wait(5)
        vllm.httpd.shutdown()


# ------------------------------------------------------------------ local guard


def gi(**kw):
    base = dict(now=10_000.0, pod_live=True, session_started=0.0, ready=True,
                limits={"idle_timeout_s": 1800, "max_session_s": 28800, "max_spend_usd": 10.0},
                supervisor={"idle_for_s": 60}, supervisor_last_seen=9_990.0, cost_per_hr=0.5, shutdown_seen_at=None)
    base.update(kw)
    return GuardInputs(**base)


def test_guard_quiet_when_healthy():
    assert decide(gi()) is None
    assert decide(gi(pod_live=False, supervisor=None)) is None


def test_guard_idle_backstop():
    assert decide(gi(supervisor={"idle_for_s": 1800 + GRACE_S + 1}))[0] == "idle_timeout"


def test_guard_catches_supervisor_that_could_not_stop_pod():
    assert decide(gi(shutdown_seen_at=10_000.0 - GRACE_S - 1))[0] == "supervisor_shutdown_stuck"


def test_guard_session_and_spend_caps():
    assert decide(gi(now=28800 + GRACE_S + 1, supervisor_last_seen=28800 + GRACE_S))[0] == "max_session"
    assert decide(gi(cost_per_hr=5.0, now=7300, supervisor_last_seen=7300))[0] == "max_spend"


def test_guard_blind_supervisor_is_not_trusted():
    assert decide(gi(supervisor=None, supervisor_last_seen=10_000.0 - 1000))[0] == "supervisor_unreachable"
    assert decide(gi(supervisor=None, supervisor_last_seen=10_000.0 - 100)) is None
