"""End-to-end benchmark run against a scripted fake endpoint (no GPU, real harness)."""

import json
import shutil

import pytest

from qwenbench.bench import report as rep
from qwenbench.bench.costs import compute_costs
from qwenbench.bench.runner import run_benchmark, write_json
from qwenbench.bench.suite import load_suite
from qwenbench.metrics.llm import ChatClient
from qwenbench.providers.base import Endpoint
from tests.conftest import FAKE_HF_TOKEN, FAKE_RUNPOD_KEY
from tests.fakes import FakeOpenAIServer, Turn, tool_call

pytestmark = [pytest.mark.slow, pytest.mark.skipif(shutil.which("uv") is None, reason="fixture setup uses uv")]

FIX = dict(path="src/ledgerlite/pagination.py", old_string="        return self.total_items // self.per_page",
           new_string="        return -(-self.total_items // self.per_page)")


def script():
    return [
        # trial 1: explore, fix, test, finish -> first-pass success
        Turn(tool_calls=[tool_call("list_files", pattern="*.py")]),
        Turn(tool_calls=[tool_call("read_file", path="src/ledgerlite/pagination.py")]),
        Turn(status=503),  # transient: retried and recorded
        Turn(tool_calls=[tool_call("edit_file", **FIX)]),
        Turn(tool_calls=[tool_call("run_command", command=".venv/bin/python -m pytest -q")]),
        Turn(tool_calls=[tool_call("finish", status="completed", summary="ceil division in total_pages")]),
        # trial 2 attempt 1: claims success without changing anything -> validation fails
        Turn(tool_calls=[tool_call("finish", status="completed", summary="looks fine to me")]),
        # trial 2 attempt 2: gets the validation feedback, fixes it
        Turn(tool_calls=[tool_call("edit_file", **FIX)]),
        Turn(tool_calls=[tool_call("finish", status="completed", summary="fixed after feedback")]),
    ]


def fast_client(ep, sink, context):
    c = ChatClient(ep.base_url, ep.api_key, ep.model, sink=sink, max_retries=3, context=context)
    c.sleep = lambda s: None
    return c


@pytest.fixture(scope="module")
def e2e_run(tmp_path_factory):
    import os
    os.environ["QWEN_RESULTS_DIR"] = str(tmp_path_factory.mktemp("results"))
    os.environ["RUNPOD_API_KEY"] = FAKE_RUNPOD_KEY
    os.environ["HF_TOKEN"] = FAKE_HF_TOKEN
    from qwenbench.config import load_config

    cfg = load_config()
    profile = cfg.profile("a6000")
    suite, cases = load_suite("smoke")
    with FakeOpenAIServer(script()) as srv:
        ep = Endpoint("a6000", srv.base_url, srv.api_key, srv.model,
                      {"compute": "runpod-pods", "pod_id": "pod_e2e", "data_center_id": "EU-RO-1"})
        session = {"requested_at": 0, "cost_per_hr": 0.49, "cost_source": "runpod-pod-cost",
                   "startup": {"ready": {"since_request_s": 91.4}}, "pod_id": "pod_e2e"}
        run_dir = run_benchmark(cfg, profile, ep, suite, cases, repeat=2, max_attempts=2, session_public=session,
                                cost_per_hr=0.49, client_factory=fast_client, progress=lambda m: None,
                                run_id="e2e-a6000")
        requests = list(srv.requests)
    meta = json.loads((run_dir / "run.json").read_text())
    meta["costs"] = compute_costs(cfg.pricing, gpu_type_id=profile.gpu_type_id, cloud="SECURE", gpu_count=1,
                                  observed_rate=0.49, observed_rate_source="runpod-pod-cost",
                                  lifetime_s=meta["benchmark_wall_s"] + 91.4, storage_gb=60,
                                  storage_mode="network-volume").to_dict()
    write_json(run_dir / "run.json", meta)
    yield run_dir, srv, requests
    os.environ.pop("QWEN_RESULTS_DIR", None)


def test_outcomes_first_pass_and_retry(e2e_run):
    run_dir, _, _ = e2e_run
    t1 = json.loads((run_dir / "tasks/fix-pagination-regression/trial-1/result.json").read_text())
    t2 = json.loads((run_dir / "tasks/fix-pagination-regression/trial-2/result.json").read_text())
    assert t1["success"] and t1["first_pass"] and len(t1["attempts"]) == 1
    assert t2["success"] and not t2["first_pass"] and len(t2["attempts"]) == 2
    assert not t2["attempts"][0]["passed"]
    assert t1["files_changed"] == [{"path": "src/ledgerlite/pagination.py", "status": "modified", "added": 1,
                                    "deleted": 1}]
    assert t1["retries"] == 1 and t1["failed_requests"] == 1
    assert t1["sandbox"] in ("macos-seatbelt", "linux-bwrap")


def test_validation_feedback_reached_the_model(e2e_run):
    _, _, requests = e2e_run
    retry_prompt = requests[-2]["messages"][1]["content"]
    assert "Feedback from the previous attempt" in retry_prompt and "test_last_partial_page_is_reachable" in retry_prompt


def test_diff_and_logs_recorded(e2e_run):
    run_dir, _, _ = e2e_run
    trial = run_dir / "tasks/fix-pagination-regression/trial-1"
    diff = (trial / "diff.patch").read_text()
    assert "-(-self.total_items // self.per_page)" in diff and "test_pagination_hidden" not in diff
    assert (trial / "setup.log").exists() and (trial / "attempt-1/validation.log").read_text()
    tools = [json.loads(line) for line in (trial / "attempt-1/tools.jsonl").read_text().splitlines()]
    assert [t["tool"] for t in tools] == ["list_files", "read_file", "edit_file", "run_command", "finish"]


def test_request_metrics(e2e_run):
    run_dir, srv, _ = e2e_run
    reqs = [json.loads(line) for line in (run_dir / "requests.jsonl").read_text().splitlines()]
    ok = [r for r in reqs if r["ok"]]
    assert len(reqs) == 9 and len(ok) == 8
    r = ok[0]
    assert r["context"]["run_id"] == "e2e-a6000" and r["context"]["task_id"] == "fix-pagination-regression"
    assert r["context"]["trial"] == 1 and r["context"]["iteration"] == 1
    for k in ("ttft_s", "total_s", "input_tokens", "output_tokens", "cached_input_tokens", "output_tokens_per_s"):
        assert r[k] is not None, k
    assert reqs[2]["http_status"] == 503 and reqs[3]["attempt"] == 2


def test_run_metadata_captures_comparability_fields(e2e_run):
    run_dir, _, _ = e2e_run
    m = json.loads((run_dir / "run.json").read_text())
    assert m["model"]["revision"] == "dcaee4d4dfc5ee71ad501f01f530e5652438fde0"
    assert m["model"]["max_model_len"] == 65536
    assert m["runtime"]["vllm_version"] == "0.30.0" and "@sha256:" in m["runtime"]["image"]
    assert "--enable-prefix-caching" in m["runtime"]["vllm_argv"]
    assert m["compute"]["gpu_type_id"] == "NVIDIA RTX A6000"
    assert m["generation"]["seed"] == 1234
    assert m["agent"]["git_sha"] is None or len(m["agent"]["git_sha"]) == 40
    case = m["cases"][0]
    assert case["prompt_sha256"] and len(set(case["starting_commits"].values())) == 1


def test_no_secrets_anywhere_in_results(e2e_run):
    run_dir, srv, _ = e2e_run
    for f in run_dir.rglob("*"):
        if f.is_file():
            text = f.read_text(errors="ignore")
            for secret in (FAKE_RUNPOD_KEY, FAKE_HF_TOKEN, srv.api_key):
                assert secret not in text, f"{secret[:6]}... leaked into {f}"


def test_report_and_compare(e2e_run):
    run_dir, _, _ = e2e_run
    run = rep.load_run(run_dir)
    s = rep.summarize(run)
    assert (s["tasks"], s["succeeded"], s["first_pass"]) == (2, 2, 1)
    assert s["gpu_startup_s"] == 91.4 and s["median_ttft_s"] is not None
    assert s["total_cost_usd"] > 0 and s["cost_per_success_usd"] == round(s["total_cost_usd"] / 2, 4)
    head = rep.headline(run)
    for label in ("First-pass success: 1", "GPU startup:", "Median TTFT:", "Cost/success:", "estimate"):
        assert label in head
    md = rep.markdown_report(run)
    assert "Individual trials" in md and "Subjective evaluation" in md and "vllm serve" in md
    assert rep.tasks_csv(run).count("\n") == 3
    cmp = rep.compare_markdown([run, run])
    assert "successful tasks" in cmp and "median TTFT" in cmp
    # Identical runs differ in nothing except (possibly) an honest dirty-checkout warning.
    issues = rep.comparability([run, run])
    assert all("dirty" in i for i in issues), issues


def test_compare_flags_real_differences(e2e_run):
    run_dir, _, _ = e2e_run
    a = rep.load_run(run_dir)
    b = rep.load_run(run_dir)
    b.meta = json.loads(json.dumps(b.meta))
    b.meta["model"]["revision"] = "0" * 40
    b.meta["runtime"]["hardware_overrides"] = {"gpu_memory_utilization": 0.92}
    issues = rep.comparability([a, b])
    assert "model revision" in issues and "hardware overrides" in issues
    assert "NOT directly comparable" in rep.compare_markdown([a, b])
