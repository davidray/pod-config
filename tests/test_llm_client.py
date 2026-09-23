import pytest

from qwenbench.metrics.llm import ChatClient, JsonlSink, LLMError
from tests.fakes import FakeOpenAIServer, Turn, tool_call


def client(srv, **kw):
    c = ChatClient(srv.base_url, srv.api_key, srv.model, **kw)
    c.sleep = lambda s: None
    return c


def test_streamed_tool_calls_are_reassembled_and_measured():
    turn = Turn(content="Looking.", tool_calls=[tool_call("read_file", path="src/a.py"),
                                                tool_call("search", pattern="def \\w+", glob="*.py")])
    with FakeOpenAIServer([turn]) as srv:
        r = client(srv).chat([{"role": "user", "content": "hi"}], tools=[], params={"temperature": 0.7, "seed": 1})
    assert r.content == "Looking."
    assert [t["function"]["name"] for t in r.tool_calls] == ["read_file", "search"]
    assert '"pattern": "def \\\\w+"' in r.tool_calls[1]["function"]["arguments"]
    rec = r.record
    assert rec.ok and rec.ttft_s is not None and rec.total_s >= rec.ttft_s
    assert rec.input_tokens == 1200 and rec.cached_input_tokens == 800 and rec.output_tokens > 0
    assert rec.served_model == srv.model and rec.tool_calls == 2 and rec.finish_reason == "tool_calls"
    assert srv.requests[0]["stream"] is True and srv.requests[0]["stream_options"] == {"include_usage": True}
    assert srv.requests[0]["seed"] == 1


def test_retries_are_recorded(tmp_path):
    with FakeOpenAIServer([Turn(status=503), Turn(status=502), Turn(content="ok")]) as srv:
        sink = JsonlSink(tmp_path / "r.jsonl", extra_secrets=[srv.api_key])
        r = client(srv, sink=sink, context={"run_id": "R"}).chat([{"role": "user", "content": "x"}])
    assert r.retries == 2
    lines = (tmp_path / "r.jsonl").read_text().splitlines()
    assert len(lines) == 3 and '"attempt": 3' in lines[2] and '"http_status": 503' in lines[0]
    assert srv.api_key not in (tmp_path / "r.jsonl").read_text()


def test_non_retryable_error_raises_immediately():
    with FakeOpenAIServer([Turn(status=400), Turn(content="never")]) as srv, pytest.raises(LLMError) as e:
        client(srv).chat([{"role": "user", "content": "x"}])
    assert len(e.value.records) == 1 and srv.calls == 1


def test_bad_key_is_not_retried_forever():
    with FakeOpenAIServer([Turn(content="x")]) as srv:
        c = ChatClient(srv.base_url, "wrong-key-000000", srv.model)
        with pytest.raises(LLMError, match="401"):
            c.chat([{"role": "user", "content": "x"}])
