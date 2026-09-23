import json

import httpx
import pytest

from qwenbench.readiness import Phase
from qwenbench.runpod import podspec
from qwenbench.runpod.client import RunpodClient, RunpodError
from qwenbench.runpod.models import Pod
from qwenbench.runpod.provider import GuardViolation, ProvisionError, RunpodPodsProvider, UpOptions
from qwenbench.state import load_session, read_events
from tests.conftest import FAKE_HF_TOKEN
from tests.fakes import FakeRunpod, pod_json


def client_for(fake: FakeRunpod) -> RunpodClient:
    return RunpodClient("rpa_x" * 5, transport=fake.transport(), max_retries=1)


# ------------------------------------------------------------------ API parsing


def test_pod_parsing_matches_v2_schema():
    p = Pod.from_api(pod_json("pod_9", status="RUNNING", cost=0.49))
    assert p.status == "RUNNING" and p.is_live and p.cost_per_hr == 0.49
    assert p.gpu_type_id == "NVIDIA RTX A6000" and p.data_center_id == "CA-MTL-3"
    assert p.proxy_url(8000) == "https://pod_9-8000.proxy.runpod.net"
    assert p.tcp_url(8000) == "http://203.0.113.7:40123"
    exited = Pod.from_api(pod_json(status="EXITED"))
    assert not exited.is_live and exited.cost_per_hr == 0.0 and exited.tcp_url(8000) is None


def test_list_pods_paginates():
    pages = [
        {"pods": [pod_json("a")], "pagination": {"nextCursor": "c1", "hasNextPage": True}},
        {"pods": [pod_json("b")], "pagination": {"nextCursor": None, "hasNextPage": False}},
    ]
    seen = []

    def handler(req):
        seen.append(req.url.params.get("cursor"))
        return httpx.Response(200, json=pages[len(seen) - 1])

    c = RunpodClient("k" * 20, transport=httpx.MockTransport(handler))
    assert [p.id for p in c.list_pods()] == ["a", "b"] and seen == [None, "c1"]


def test_errors_are_rfc9457_and_classified():
    def handler(req):
        return httpx.Response(402, json={"title": "Payment Required", "status": 402, "detail": "balance too low"})

    with pytest.raises(RunpodError) as e:
        RunpodClient("k" * 20, transport=httpx.MockTransport(handler)).create_pod({})
    assert e.value.is_insufficient_balance and "balance too low" in str(e.value)


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"title": "slow", "status": 429, "detail": ""})
        return httpx.Response(200, json=pod_json("x"))

    assert RunpodClient("k" * 20, transport=httpx.MockTransport(handler)).get_pod("x").id == "x"
    assert len(calls) == 2


def test_terminate_is_idempotent():
    fake = FakeRunpod()
    c = client_for(fake)
    fake.pods["p"] = {**pod_json("p"), "_status_iter": ["RUNNING"]}
    assert c.terminate_pod("p") is True
    assert c.terminate_pod("p") is False  # 404 -> already gone, not an error


def test_logs_sse():
    fake = FakeRunpod()
    lines = list(client_for(fake).stream_logs("p"))
    assert [x["line"] for x in lines] == ["line 0", "line 1", "line 2"]


def test_billing_and_volumes():
    fake = FakeRunpod()
    c = client_for(fake)
    v = c.create_network_volume("qwenbench-hf-cache-a6000", 60, "EU-RO-1")
    assert [x.name for x in c.list_network_volumes()] == ["qwenbench-hf-cache-a6000"] and v.data_center == "EU-RO-1"
    assert c.pod_billing("pod_1", "2026-09-23T00:00:00Z")["metadata"]["totals"]["totalAmount"] == 0.41


# ------------------------------------------------------------------ pod spec


def test_create_body_shape(cfg):
    p = cfg.profile("a6000")
    env = podspec.pod_env(p, endpoint_api_key="k" * 30, list_cost_per_hr=0.53, hf_token=FAKE_HF_TOKEN)
    body = podspec.create_body(p, env=env, data_center_ids=["EU-RO-1"], network_volume_id="vol_1")
    assert body["gpu"] == {"id": "NVIDIA RTX A6000", "count": 1, "minCudaVersion": "12.9"}
    assert body["mounts"] == {"network": [{"volumeId": "vol_1", "path": "/workspace"}]}
    assert body["ports"] == ["8000/http", "8001/http"] and body["cloud"] == "SECURE"
    assert body["entrypoint"] == ["/bin/bash", "-c"]
    argv = json.loads(body["env"]["QWENBENCH_VLLM_ARGV"])
    assert argv[:3] == ["vllm", "serve", "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"]
    assert "--enable-prefix-caching" in argv and argv[argv.index("--max-model-len") + 1] == "65536"
    assert argv[argv.index("--revision") + 1] == p.model.revision
    assert len(json.dumps(body)) < 100_000  # Runpod v2 rejects bodies over 100 KB (413)
    committed = podspec.rendered_for_repo(body)
    assert FAKE_HF_TOKEN not in json.dumps(committed) and "k" * 30 not in json.dumps(committed)


def test_bootstrap_archive_is_deterministic():
    assert podspec.bootstrap_archive() == podspec.bootstrap_archive()


def test_ephemeral_storage_sizes_disk(cfg):
    p = cfg.profile("l40s")
    p = p.model_copy(update={"storage": p.storage.model_copy(update={"mode": "ephemeral"})})
    body = podspec.create_body(p, env={}, data_center_ids=[], network_volume_id=None)
    assert "mounts" not in body and body["disk"] == p.container_disk_gb + p.storage.size_gb


# ------------------------------------------------------------------ provider lifecycle


def ready_transport(model="qwen3-coder-30b-a3b-fp8", phase="server_up"):
    """Endpoint + supervisor answering through the pod proxy URLs."""
    def handler(req: httpx.Request):
        path = req.url.path
        if req.url.host.endswith("-8001.proxy.runpod.net") and path == "/status":
            return httpx.Response(200, json={"phase": phase, "cost_per_hr": 0.49, "cost_source": "runpod-pod-cost"})
        if path == "/health":
            return httpx.Response(200)
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": model, "root": "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"}]})
        if path == "/v1/chat/completions":
            chunk = {"model": model, "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]}
            usage = {"model": model, "choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 1}}
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(usage)}\n\ndata: [DONE]\n\n",
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def provider(cfg, fake, endpoint_transport, clock=None):
    clock = clock or Clock()
    prov = RunpodPodsProvider(cfg, client=client_for(fake), transport=endpoint_transport, sleep=clock.sleep, clock=clock)
    return prov, clock


@pytest.fixture
def patch_model_transport(monkeypatch):
    """OpenAICompatibleModel.probe builds its own ChatClient; route it through the same mock."""
    def install(transport):
        import qwenbench.providers.openai_compat as oc
        from qwenbench.metrics import llm
        real_init = llm.ChatClient.__post_init__

        def post_init(self):
            self.transport = self.transport or transport
            real_init(self)
        monkeypatch.setattr(llm.ChatClient, "__post_init__", post_init)
        return oc
    return install


def test_up_creates_volume_pod_and_waits_for_real_inference(cfg, patch_model_transport):
    fake = FakeRunpod(capacity_errors=set())
    t = ready_transport()
    patch_model_transport(t)
    prov, clock = provider(cfg, fake, t)
    msgs = []
    ep = prov.up(cfg.profile("a6000"), UpOptions(idle_timeout_s=3600.0), progress=msgs.append)
    assert ep.base_url == "https://pod_1-8000.proxy.runpod.net/v1" and ep.model == "qwen3-coder-30b-a3b-fp8"
    # volume discovered/created by name, in the best-availability data center
    vol = next(iter(fake.volumes.values()))
    assert vol["name"] == "qwenbench-hf-cache-a6000" and vol["dataCenter"] == "EU-RO-1"
    body = fake.created[0]
    assert body["dataCenterIds"] == ["EU-RO-1"] and body["env"]["QWENBENCH_IDLE_TIMEOUT_S"] == "3600"
    s = load_session("a6000")
    assert s.ready_at and s.startup["ready"]["since_request_s"] >= 0 and s.cost_source == "runpod-pod-cost"
    assert any("endpoint ready" in m for m in msgs)
    assert any(e["event"] == "pod-ready" for e in read_events())


def test_up_is_idempotent_for_a_ready_session(cfg, patch_model_transport):
    fake = FakeRunpod()
    t = ready_transport()
    patch_model_transport(t)
    prov, _ = provider(cfg, fake, t)
    prov.up(cfg.profile("a6000"))
    prov.up(cfg.profile("a6000"))
    assert len(fake.created) == 1


def test_guard_refuses_second_gpu(cfg, patch_model_transport):
    fake = FakeRunpod()
    t = ready_transport()
    patch_model_transport(t)
    prov, _ = provider(cfg, fake, t)
    prov.up(cfg.profile("a6000"))
    with pytest.raises(GuardViolation, match="other qwenbench pods are live"):
        prov.up(cfg.profile("l40s"))
    with pytest.raises(GuardViolation, match="max_gpu_count"):
        prov.up(cfg.profile("l40s"), UpOptions(allow_concurrent=True))
    assert len(fake.created) == 1


def test_wrong_model_identity_never_becomes_ready_and_is_cleaned_up(cfg, patch_model_transport):
    fake = FakeRunpod()
    t = ready_transport(model="some-other-model")
    patch_model_transport(t)
    prov, clock = provider(cfg, fake, t)
    with pytest.raises(ProvisionError, match="not ready"):
        prov.up(cfg.profile("a6000"), UpOptions(startup_timeout_s=120, poll_s=30))
    assert fake.terminated == ["pod_1"] and load_session("a6000") is None


def test_pod_error_during_startup_terminates(cfg, patch_model_transport):
    fake = FakeRunpod(statuses=["PROVISIONING", "ERROR"])
    t = ready_transport()
    patch_model_transport(t)
    prov, _ = provider(cfg, fake, t)
    with pytest.raises(ProvisionError, match="ERROR"):
        prov.up(cfg.profile("a6000"))
    assert fake.terminated == ["pod_1"]
    assert any(e["event"] == "pod-terminated" and "startup-failed" in e["reason"] for e in read_events())


def test_capacity_error_tries_next_data_center_in_ephemeral_mode(cfg, patch_model_transport):
    fake = FakeRunpod(capacity_errors={"EU-RO-1"})
    t = ready_transport()
    patch_model_transport(t)
    prov, _ = provider(cfg, fake, t)
    p = cfg.profile("a6000")
    p = p.model_copy(update={"storage": p.storage.model_copy(update={"mode": "ephemeral"})})
    prov.up(p)
    assert fake.created[0]["dataCenterIds"] == ["CA-MTL-3"]


def test_capacity_error_with_volume_explains(cfg, patch_model_transport):
    fake = FakeRunpod(capacity_errors={"EU-RO-1"})
    t = ready_transport()
    patch_model_transport(t)
    prov, _ = provider(cfg, fake, t)
    with pytest.raises(ProvisionError, match="ephemeral"):
        prov.up(cfg.profile("a6000"))
    assert not fake.pods


def test_down_is_idempotent_and_down_all(cfg, patch_model_transport):
    fake = FakeRunpod()
    t = ready_transport()
    patch_model_transport(t)
    prov, _ = provider(cfg, fake, t)
    prov.up(cfg.profile("a6000"))
    fake.pods["stray"] = {**pod_json("stray", name="qwenbench-l40s"), "_status_iter": ["RUNNING"]}
    fake.pods["theirs"] = {**pod_json("theirs", name="someone-elses-pod"), "_status_iter": ["RUNNING"]}
    assert prov.down(cfg.profile("a6000")) == ["pod_1"]
    assert prov.down(cfg.profile("a6000")) == []
    assert prov.down_all() == ["stray"]
    assert "theirs" in fake.pods  # never touch pods we did not create
    assert load_session("a6000") is None


def test_status_estimates_spend(cfg, patch_model_transport):
    fake = FakeRunpod()
    t = ready_transport()
    patch_model_transport(t)
    prov, clock = provider(cfg, fake, t)
    prov.up(cfg.profile("a6000"))
    st = prov.status(cfg.profile("a6000"))
    assert st.live and st.cost_per_hr == 0.49 and st.estimated_spend_usd is not None
    assert prov.status(cfg.profile("l40s")).exists is False


def test_readiness_phases_reported(cfg, patch_model_transport):
    fake = FakeRunpod()
    t = ready_transport()
    patch_model_transport(t)
    prov, _ = provider(cfg, fake, t)
    prov.up(cfg.profile("a6000"))
    phases = load_session("a6000").startup
    assert {"requested", "allocated", "model_loaded", "ready"} <= set(phases)
    assert Phase.READY.name.lower() in phases


def test_no_stock_does_not_create_a_billed_volume(cfg, patch_model_transport):
    fake = FakeRunpod()
    fake.gpu_availability["NVIDIA RTX A6000"] = "NONE"
    t = ready_transport()
    patch_model_transport(t)
    prov, _ = provider(cfg, fake, t)
    with pytest.raises(ProvisionError, match="no availability"):
        prov.up(cfg.profile("a6000"))
    assert not fake.volumes and not fake.created


def test_ephemeral_falls_back_to_any_data_center(cfg, patch_model_transport):
    fake = FakeRunpod(capacity_errors={"EU-RO-1", "CA-MTL-3"})
    t = ready_transport()
    patch_model_transport(t)
    prov, _ = provider(cfg, fake, t)
    p = cfg.profile("a6000")
    p = p.model_copy(update={"storage": p.storage.model_copy(update={"mode": "ephemeral"})})
    prov.up(p)
    assert fake.created[0]["dataCenterIds"] == []  # scheduler's choice
