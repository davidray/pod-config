# Dispatch protocol (`qwenbench.dispatch/v1`)

The contract between an orchestrator (Claude in a Hero workflow, or the
benchmark harness) and an implementation worker (the Qwen agent today). The
schema lives in `src/qwenbench/agent/protocol.py`.

## Request

```json
{
  "protocol": "qwenbench.dispatch/v1",
  "role": "api-engineer",
  "task": "Implement GET /v1/invoices/{id}/pdf per the spec below ...",
  "repo_path": "/Users/you/code/myproject",
  "context": "Spec excerpt, conventions to follow, decisions, files to touch ...",
  "acceptance_criteria": ["returns 404 for unknown ids", "streams application/pdf"],
  "validation_commands": ["bundle exec rspec spec/requests/invoices_spec.rb"],
  "spec_ref": "invoice-pdf-download",
  "profile": null,
  "limits": {"max_iterations": 60, "timeout_s": 1800, "max_total_output_tokens": 200000, "command_timeout_s": 600},
  "attempt": 1,
  "allow_network": false
}
```

CLI form (what Claude runs): `qwenbench dispatch --project DIR --role R --task-file
task.md [--context-file ctx.md] [--criteria ...]... [--validate CMD]...
[--spec SLUG]`, or `--request request.json`.

## Result

```json
{
  "protocol": "qwenbench.dispatch/v1",
  "dispatch_id": "d-20260923T101500-a1b2c3",
  "status": "completed | failed | blocked | error",
  "summary": "what the agent says it did",
  "agent_notes": "caveats",
  "role": "api-engineer",
  "files_changed": [{"path": "app/controllers/invoices_controller.rb", "status": "modified", "added": 24, "deleted": 2}],
  "lines_added": 24, "lines_deleted": 2,
  "validation": [{"command": "...", "passed": true, "exit_code": 0, "duration_s": 12.3, "output_tail": "..."}],
  "metrics": {"requests": 17, "input_tokens": 212000, "cached_input_tokens": 180000, "output_tokens": 6100,
              "median_ttft_s": 0.9, "median_output_tps": 61.2, "iterations": 17, "tool_calls": 22,
              "agent_wall_s": 340.1, "retries": 0, "failed_requests": 0},
  "model": {"provider": "qwen", "profile": "a6000", "endpoint_model": "qwen3-coder-30b-a3b-fp8",
            "served_models_observed": ["qwen3-coder-30b-a3b-fp8"], "hf_repo": "...", "revision": "...",
            "pod_id": "...", "gpu_type_id": "NVIDIA RTX A6000", "route": {"...": "..."}},
  "failure": null,
  "base_tree": "git tree before", "result_tree": "git tree after",
  "artifacts": {"diff": "...", "transcript": "...", "tools": "...", "requests": "..."}
}
```

`status` is decided by the harness, not the model:

- **completed:** the agent called `finish(completed)` **and** every validation command passed.
- **failed:** covers `validation_failed`, `agent_gave_up`, `iteration_limit`, `timeout`, `token_budget` and `no_progress`.
- **blocked:** the agent reported it needs information (`agent_blocked`).
- **error:** the work could not be attempted or the endpoint failed (`endpoint_unavailable`, `llm_error`, `policy`). `failure.retryable` says whether retrying can help.

Exit codes: 0 completed, 1 failed or blocked, 3 policy refusal, 4 endpoint
unavailable, 5 attempt budget exhausted.

## Substituting another worker

Any worker that accepts the request and returns the result can replace
`agent/loop.py`. Examples: a different local or cloud model behind an
OpenAI-compatible API, or a different agent framework wrapped in a small
adapter. Routing, benchmarking and auditing depend only on this contract and
on `Endpoint`.
