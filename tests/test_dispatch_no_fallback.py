"""The core guarantee: implementation work goes to Qwen or fails loudly. Never to Claude."""

import json
import subprocess

import pytest

from qwenbench.agent.protocol import DispatchRequest
from qwenbench.hero.dispatch import run_dispatch
from qwenbench.hero.ledger import read_jsonl
from qwenbench.metrics.llm import ChatClient
from qwenbench.providers.base import Endpoint
from tests.fakes import FakeOpenAIServer, Turn, tool_call


class FakeProvider:
    def __init__(self, endpoint):
        self._ep = endpoint

    def endpoint(self, profile):
        return self._ep


def req(project, role="engineer", task="Fix add() so it adds.", validate=None):
    return DispatchRequest(role=role, task=task, repo_path=str(project),
                           validation_commands=validate or ["python3 -c 'import sys; sys.path.insert(0, \"src\"); "
                                                             "import app; assert app.add(2, 3) == 5'"])


def solving_script():
    return [
        Turn(tool_calls=[tool_call("read_file", path="src/app.py")]),
        Turn(tool_calls=[tool_call("edit_file", path="src/app.py", old_string="return a - b", new_string="return a + b")]),
        Turn(tool_calls=[tool_call("run_command", command="python3 -c 'import sys; sys.path.insert(0, \"src\"); "
                                                          "import app; print(app.add(2, 3))'")]),
        Turn(tool_calls=[tool_call("finish", status="completed", summary="add() now adds")]),
    ]


def test_successful_dispatch_is_attributed_to_qwen(cfg, configured_project):
    with FakeOpenAIServer(solving_script()) as srv:
        ep = Endpoint("a6000", srv.base_url, srv.api_key, srv.model, {"pod_id": "pod_x", "gpu_type_id": "NVIDIA RTX A6000"})
        result, code = run_dispatch(cfg, req(configured_project), configured_project,
                                    provider_factory=lambda c: FakeProvider(ep))
    assert code == 0 and result.status == "completed", result.failure
    assert [f.path for f in result.files_changed] == ["src/app.py"]
    assert result.model["served_models_observed"] == [srv.model]
    assert result.model["route"]["model_id"] == "qwen:a6000" and result.model["pod_id"] == "pod_x"
    assert result.metrics["requests"] == 4 and result.metrics["tool_calls"] == 4
    end = [r for r in read_jsonl(configured_project, "ledger.jsonl") if r["event"] == "dispatch-end"][-1]
    assert end["files"][0]["path"] == "src/app.py" and end["files"][0]["blob"]
    # Every request went to the Qwen endpoint; there is no other model client in the path.
    assert all(r["model"] == srv.model for r in srv.requests)
    # The endpoint token never lands in the evidence files.
    for f in (configured_project / ".qwen-routing").rglob("*"):
        if f.is_file():
            assert srv.api_key not in f.read_text(errors="ignore"), f


def test_audit_attributes_dispatch_changes(cfg, configured_project, monkeypatch, capsys):
    from qwenbench.hero import hooks
    from qwenbench.hero.audit import unattributed_changes

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(configured_project))
    hooks.main("session-start", json.dumps({"session_id": "s", "cwd": str(configured_project)}))
    with FakeOpenAIServer(solving_script()) as srv:
        ep = Endpoint("a6000", srv.base_url, srv.api_key, srv.model)
        run_dispatch(cfg, req(configured_project), configured_project, provider_factory=lambda c: FakeProvider(ep))
    assert unattributed_changes(configured_project, cfg.policy) == []
    (configured_project / "src" / "app.py").write_text("tampered by someone else\n")
    assert unattributed_changes(configured_project, cfg.policy) == ["src/app.py"]


def test_endpoint_down_fails_fast_without_touching_code(cfg, configured_project):
    before = (configured_project / "src" / "app.py").read_text()
    result, code = run_dispatch(cfg, req(configured_project), configured_project,
                                provider_factory=lambda c: FakeProvider(None))
    assert code == 4 and result.status == "error"
    assert result.failure.kind == "endpoint_unavailable"
    assert "Do not fall back" in result.failure.message
    assert (configured_project / "src" / "app.py").read_text() == before


def test_unhealthy_endpoint_fails_fast(cfg, configured_project):
    ep = Endpoint("a6000", "http://127.0.0.1:9/v1", "k" * 20, "qwen3-coder-30b-a3b-fp8")  # nothing listens
    result, code = run_dispatch(cfg, req(configured_project), configured_project,
                                provider_factory=lambda c: FakeProvider(ep))
    assert code == 4 and result.failure.kind == "endpoint_unavailable"


def test_endpoint_dying_mid_task_is_an_error_not_a_success(cfg, configured_project):
    script = [Turn(tool_calls=[tool_call("read_file", path="src/app.py")])] + [Turn(status=503)] * 10
    with FakeOpenAIServer(script) as srv:
        ep = Endpoint("a6000", srv.base_url, srv.api_key, srv.model)

        def client_factory(*a, **k):
            c = ChatClient(*a, **k)
            c.sleep = lambda s: None
            return c

        result, code = run_dispatch(cfg, req(configured_project), configured_project,
                                    provider_factory=lambda c: FakeProvider(ep), client_factory=client_factory)
    assert result.status == "error" and code == 4
    assert result.failure.kind == "endpoint_unavailable" and result.failure.retryable
    assert result.metrics["retries"] >= 3  # retries are counted, never hidden


def test_validation_failure_is_reported_even_if_model_claims_success(cfg, configured_project):
    script = [Turn(tool_calls=[tool_call("finish", status="completed", summary="all good, trust me")])]
    with FakeOpenAIServer(script) as srv:
        ep = Endpoint("a6000", srv.base_url, srv.api_key, srv.model)
        result, code = run_dispatch(cfg, req(configured_project), configured_project,
                                    provider_factory=lambda c: FakeProvider(ep))
    assert code == 1 and result.status == "failed" and result.failure.kind == "validation_failed"


def test_frontier_role_cannot_be_dispatched(cfg, configured_project):
    result, code = run_dispatch(cfg, req(configured_project, role="brownfield-architect"), configured_project,
                                provider_factory=lambda c: pytest.fail("must not reach a provider"))
    assert code == 3 and result.failure.kind == "policy" and "natively" in result.failure.message


def test_attempt_budget_forces_escalation(cfg, configured_project):
    script = lambda body: Turn(tool_calls=[tool_call("finish", status="failed", summary="cannot")])  # noqa: E731
    with FakeOpenAIServer(script) as srv:
        ep = Endpoint("a6000", srv.base_url, srv.api_key, srv.model)
        budget = cfg.policy.enforcement.dispatch.max_attempts_per_task
        codes = [run_dispatch(cfg, req(configured_project), configured_project,
                              provider_factory=lambda c: FakeProvider(ep))[1] for _ in range(budget)]
        last, code = run_dispatch(cfg, req(configured_project), configured_project,
                                  provider_factory=lambda c: FakeProvider(ep))
    assert codes == [1] * budget
    assert code == 5 and "surface the failure to the human" in last.failure.message
    assert "must not implement" in last.failure.message


def test_agent_cannot_write_routing_config_during_dispatch(cfg, configured_project):
    script = [
        Turn(tool_calls=[tool_call("write_file", path=".qwen-routing/config.json", content='{"enforce": false}')]),
        Turn(tool_calls=[tool_call("run_command", command="echo '{}' > .claude/settings.local.json")]),
        Turn(tool_calls=[tool_call("finish", status="failed", summary="tried to disable routing")]),
    ]
    with FakeOpenAIServer(script) as srv:
        ep = Endpoint("a6000", srv.base_url, srv.api_key, srv.model)
        run_dispatch(cfg, req(configured_project), configured_project, provider_factory=lambda c: FakeProvider(ep))
    assert json.loads((configured_project / ".qwen-routing" / "config.json").read_text())["enforce"] is True
    assert "qwenbench" in (configured_project / ".claude" / "settings.local.json").read_text()


def test_cli_dispatch_exit_code_and_json(configured_project):
    p = subprocess.run(["uv", "run", "--quiet", "qwen", "dispatch", "--project", str(configured_project),
                        "--role", "brownfield-architect", "--task", "design it"], capture_output=True, text=True)
    assert p.returncode == 3
    assert json.loads(p.stdout)["failure"]["kind"] == "policy"
