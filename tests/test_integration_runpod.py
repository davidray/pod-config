"""Opt-in tests against the real Runpod API.

    QWEN_RUNPOD_INTEGRATION=readonly  catalog/auth checks (no spend)
    QWEN_RUNPOD_INTEGRATION=1         ALSO provisions a real GPU pod (spends money),
                                      runs a probe + chat, and always terminates it.
    QWEN_IT_PROFILE=a6000|l40s        profile for the paid test (default a6000)

Requires a real RUNPOD_API_KEY (the autouse fixture fakes it unless
QWEN_IT_REAL_KEY holds the real one).
"""

import os

import pytest

MODE = os.environ.get("QWEN_RUNPOD_INTEGRATION", "")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(MODE not in ("readonly", "1"), reason="set QWEN_RUNPOD_INTEGRATION=readonly or 1")]


@pytest.fixture
def real_key(monkeypatch):
    key = os.environ.get("QWEN_IT_REAL_KEY")
    if not key:
        pytest.skip("export QWEN_IT_REAL_KEY=<your runpod key> for integration tests")
    monkeypatch.setenv("RUNPOD_API_KEY", key)
    return key


def test_catalog_has_profile_gpus(cfg, real_key):
    from qwenbench.runpod.provider import RunpodPodsProvider

    prov = RunpodPodsProvider(cfg)
    for name in cfg.profile_names():
        p = cfg.profile(name)
        cat = prov.client.gpu_catalog(p.gpu_type_id, cloud=p.cloud)
        assert cat and cat[0]["id"] == p.gpu_type_id
    prov.our_pods()  # auth works


@pytest.mark.skipif(MODE != "1", reason="paid test: set QWEN_RUNPOD_INTEGRATION=1")
def test_paid_up_probe_down(cfg, real_key):
    from qwenbench.providers.openai_compat import OpenAICompatibleModel
    from qwenbench.runpod.provider import RunpodPodsProvider, UpOptions

    prov = RunpodPodsProvider(cfg)
    p = cfg.profile(os.environ.get("QWEN_IT_PROFILE", "a6000"))
    try:
        ep = prov.up(p, UpOptions(idle_timeout_s=900.0, max_session_s=3600.0, max_spend_usd=3.0))
        probe = OpenAICompatibleModel(ep).probe()
        assert probe["ok"], probe
        st = prov.status(p)
        assert st.live and st.detail["supervisor"]["runpod_api_auth_ok"] is not None
    finally:
        prov.down(p, reason="integration test teardown")
    assert not [x for x in prov.our_pods() if x.is_live]
