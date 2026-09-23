from qwenbench.readiness import Observation, Phase, ReadinessMachine


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def machine(timeout=600):
    clock = Clock()
    return ReadinessMachine(timeout, clock=clock), clock


def test_full_progression_with_timings():
    m, c = machine()
    c.t += 5
    assert m.observe(Observation(pod_status="PROVISIONING")) == Phase.ALLOCATED
    c.t += 40
    assert m.observe(Observation(pod_status="RUNNING", supervisor_phase="container_started")) == Phase.CONTAINER_STARTED
    c.t += 2
    assert m.observe(Observation(pod_status="RUNNING", supervisor_phase="vllm_starting")) == Phase.VLLM_STARTED
    c.t += 100
    assert m.observe(Observation(pod_status="RUNNING", supervisor_phase="server_up", health_ok=True)) == Phase.MODEL_LOADED
    c.t += 1
    assert m.observe(Observation(pod_status="RUNNING", supervisor_phase="server_up", health_ok=True,
                                 probe_ok=True)) == Phase.READY
    t = m.timings()
    assert t["allocated"]["since_request_s"] == 5
    assert t["ready"]["since_request_s"] == 148
    assert not any(v["inferred"] for v in t.values())


def test_running_pod_is_not_ready():
    m, c = machine()
    # Runpod says RUNNING and even /health answers, but no real inference yet.
    assert m.observe(Observation(pod_status="RUNNING", supervisor_phase="server_up", health_ok=True)) == Phase.MODEL_LOADED
    assert m.phase != Phase.READY


def test_skipped_phases_are_marked_inferred():
    m, c = machine()
    c.t += 90
    m.observe(Observation(pod_status="RUNNING", supervisor_phase="server_up", health_ok=True))
    t = m.timings()
    assert t["container_started"]["inferred"] and t["vllm_started"]["inferred"]
    assert not t["model_loaded"]["inferred"]


def test_pod_error_fails():
    m, _ = machine()
    m.observe(Observation(pod_status="STARTING"))
    assert m.observe(Observation(pod_status="ERROR", detail="image pull")) == Phase.FAILED
    assert "ERROR" in m.failure
    # terminal: later observations do not resurrect it
    assert m.observe(Observation(pod_status="RUNNING", probe_ok=True)) == Phase.FAILED


def test_vllm_crash_fails():
    m, _ = machine()
    assert m.observe(Observation(pod_status="RUNNING", supervisor_phase="vllm_exited")) == Phase.FAILED


def test_timeout():
    m, c = machine(timeout=60)
    m.observe(Observation(pod_status="RUNNING", supervisor_phase="model_loading"))
    c.t += 61
    assert m.observe(Observation(pod_status="RUNNING", supervisor_phase="model_loading",
                                 probe_error="connection refused")) == Phase.TIMED_OUT
    assert "connection refused" in m.failure


def test_never_goes_backwards():
    m, _ = machine()
    m.observe(Observation(pod_status="RUNNING", supervisor_phase="server_up", health_ok=True))
    assert m.observe(Observation(pod_status="RUNNING", supervisor_phase="vllm_starting")) == Phase.MODEL_LOADED
