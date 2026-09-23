import json

from qwenbench.secrets import REDACTED, _parse_dotenv, get_secret, redact
from qwenbench.state import Session, load_session, log_event, save_session
from tests.conftest import FAKE_HF_TOKEN, FAKE_RUNPOD_KEY


def test_dotenv_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\nexport RUNPOD_API_KEY='abc'\nHF_TOKEN=\"x y\"\nEMPTY=\nBAD LINE\n")
    assert _parse_dotenv(p) == {"RUNPOD_API_KEY": "abc", "HF_TOKEN": "x y", "EMPTY": ""}


def test_env_wins_over_dotenv(monkeypatch):
    import qwenbench.secrets as s
    monkeypatch.setattr(s, "_dotenv_cache", {"RUNPOD_API_KEY": "from-file"})
    assert get_secret("RUNPOD_API_KEY") == FAKE_RUNPOD_KEY
    monkeypatch.delenv("RUNPOD_API_KEY")
    assert get_secret("RUNPOD_API_KEY") == "from-file"


def test_redact_values_and_keys():
    obj = {"error": f"auth failed with Bearer {FAKE_RUNPOD_KEY}", "api_key": "anything-at-all",
           "nested": [{"hf": FAKE_HF_TOKEN}], "token_count": 5, "authorization": "Bearer zzz"}
    out = redact(obj, extra_secrets=["endpoint-secret-xyz"])
    text = json.dumps(out)
    assert FAKE_RUNPOD_KEY not in text and FAKE_HF_TOKEN not in text
    assert out["api_key"] == REDACTED and out["authorization"] == REDACTED
    assert out["token_count"] == 5  # non-string values are kept
    assert redact("x endpoint-secret-xyz y", ["endpoint-secret-xyz"]) == f"x {REDACTED} y"


def _session():
    return Session(profile="a6000", compute="runpod-pods", pod_id="p1", pod_name="qwenbench-a6000",
                   endpoint_url="https://p1-8000.proxy.runpod.net/v1", watchdog_url="https://p1-8001.proxy.runpod.net",
                   api_key="per-session-bearer-token-xyz", served_model_name="m", gpu_type_id="NVIDIA RTX A6000",
                   cloud="SECURE", data_center_id="CA-MTL-3", network_volume_id="v1", requested_at=1.0)


def test_session_public_dict_drops_key_and_file_is_private(tmp_path):
    s = _session()
    save_session(s)
    assert "api_key" not in s.public_dict()
    assert load_session("a6000").api_key == s.api_key
    from qwenbench.state import session_path
    assert oct(session_path("a6000").stat().st_mode)[-3:] == "600"


def test_event_log_is_redacted():
    rec = log_event("test", detail=f"key {FAKE_RUNPOD_KEY}", token="t0ps3cr3t-value")
    assert FAKE_RUNPOD_KEY not in json.dumps(rec)
    assert rec["token"] == REDACTED
