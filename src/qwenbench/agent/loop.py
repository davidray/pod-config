"""The Qwen coding-agent loop (see docs/adr/0007 for why it is hand-rolled).

    prompt -> model -> tool calls -> sandboxed execution -> results -> model ...
    until finish() / iteration cap / wall-clock cap / token budget / LLM failure

Then the harness (not the model) runs the validation commands and decides the
outcome. Every model request is recorded through the ChatClient sink, and every
tool call is appended to tools.jsonl.
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qwenbench.agent import snapshot
from qwenbench.agent.protocol import DispatchRequest, DispatchResult, Failure, ValidationResult
from qwenbench.agent.sandbox import Sandbox
from qwenbench.agent.tools import TOOL_SCHEMAS, Toolbox
from qwenbench.metrics.llm import ChatClient, LLMError, RequestRecord
from qwenbench.secrets import redact

SYSTEM_PROMPT = """\
You are acting as the `{role}` on a software team. You implement changes in the repository \
you are given, using the provided tools. Another senior engineer designed the work; your job \
is to execute it precisely.

Working rules:
- Explore before editing: list files, search, and read the relevant code first.
- Follow the existing architecture, naming and style. Make the smallest change that fully \
satisfies the task. Do not refactor or reformat unrelated code.
- Do not weaken, skip or delete tests to make them pass unless the task explicitly says to.
- Run the validation commands yourself and fix failures before finishing.
- The sandbox has no network access; dependencies are already installed.
- Git commits are handled by the harness; never try to commit.
- When done, call `finish` exactly once with an honest status and a concise summary.
"""


def build_user_prompt(req: DispatchRequest) -> str:
    parts = [f"## Task\n{req.task.strip()}"]
    if req.context.strip():
        parts.append(f"## Context\n{req.context.strip()}")
    if req.acceptance_criteria:
        parts.append("## Acceptance criteria\n" + "\n".join(f"- {c}" for c in req.acceptance_criteria))
    if req.validation_commands:
        parts.append("## Validation (must pass before you finish)\n"
                     + "\n".join(f"- `{c}`" for c in req.validation_commands))
    return "\n\n".join(parts)


@dataclass
class AgentRun:
    """Mutable bookkeeping for one loop execution."""
    records: list[RequestRecord] = field(default_factory=list)
    tool_calls: int = 0
    iterations: int = 0
    finished: dict[str, Any] | None = None
    failure: Failure | None = None
    touched: set[str] = field(default_factory=set)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def summarize_records(records: list[RequestRecord]) -> dict[str, Any]:
    ok = [r for r in records if r.ok]
    return {
        "requests": len(records),
        "failed_requests": sum(1 for r in records if not r.ok),
        "retries": sum(1 for r in records if r.attempt > 1),
        "input_tokens": sum(r.input_tokens or 0 for r in ok),
        "output_tokens": sum(r.output_tokens or 0 for r in ok),
        "cached_input_tokens": sum(r.cached_input_tokens or 0 for r in ok),
        "median_ttft_s": _median([r.ttft_s for r in ok if r.ttft_s is not None]),
        "median_output_tps": _median([r.output_tokens_per_s for r in ok if r.output_tokens_per_s]),
        "inference_s": round(sum(r.total_s for r in records), 3),
    }


def compact_history(messages: list[dict[str, Any]], keep_last: int = 8, limit: int = 400) -> int:
    """Elide old tool outputs in place to recover context. Returns chars removed."""
    removed = 0
    cutoff = max(2, len(messages) - keep_last)
    for msg in messages[2:cutoff]:
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > limit:
            removed += len(msg["content"]) - limit
            msg["content"] = msg["content"][:limit] + f"\n...[elided {len(msg['content']) - limit} chars of old output]"
    return removed


def run_agent(
    req: DispatchRequest,
    client: ChatClient,
    workspace: Path,
    sandbox: Sandbox,
    generation: dict[str, Any],
    max_model_len: int,
    out_dir: Path,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[AgentRun, list[dict[str, Any]]]:
    """Drive the model until it finishes or a limit trips. Does not validate."""
    run = AgentRun()
    tools = Toolbox(workspace, sandbox, max_command_timeout_s=req.limits.command_timeout_s)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(role=req.role)},
        {"role": "user", "content": build_user_prompt(req)},
    ]
    original_sink = client.sink

    def sink(rec: RequestRecord) -> None:
        run.records.append(rec)
        if original_sink:
            original_sink(rec)

    client.sink = sink
    tool_log = (out_dir / "tools.jsonl").open("a")
    started = clock()
    idle_turns = 0
    try:
        while run.finished is None:
            if run.iterations >= req.limits.max_iterations:
                run.failure = Failure(kind="iteration_limit", message=f"hit {req.limits.max_iterations} iterations")
                break
            if clock() - started > req.limits.timeout_s:
                run.failure = Failure(kind="timeout", message=f"exceeded {int(req.limits.timeout_s)}s wall clock")
                break
            out_tokens = sum(r.output_tokens or 0 for r in run.records if r.ok)
            if out_tokens > req.limits.max_total_output_tokens:
                run.failure = Failure(kind="token_budget", message=f"generated {out_tokens} output tokens")
                break
            run.iterations += 1
            try:
                result = client.chat(messages, tools=TOOL_SCHEMAS, params=generation,
                                     context={"iteration": run.iterations})
            except LLMError as e:
                if "context length" in str(e).lower() and compact_history(messages, keep_last=4, limit=200):
                    continue
                run.failure = Failure(kind="endpoint_unavailable" if _is_transport(e) else "llm_error",
                                      message=str(e), retryable=True)
                break
            last_prompt = result.usage.get("prompt_tokens") or 0
            if last_prompt > 0.75 * max_model_len:
                compact_history(messages)
            messages.append(result.assistant_message())
            if not result.tool_calls:
                idle_turns += 1
                if idle_turns >= 3:
                    run.failure = Failure(kind="no_progress", message="model stopped calling tools without finishing")
                    break
                messages.append({"role": "user", "content":
                                 "Continue using the tools. When the work is complete and validated, call `finish`."})
                continue
            idle_turns = 0
            for call in result.tool_calls:
                fn = call.get("function") or {}
                t0 = time.monotonic()
                outcome = tools.call(fn.get("name", ""), fn.get("arguments") or "{}")
                run.tool_calls += 1
                if outcome.touched:
                    run.touched.add(outcome.touched)
                tool_log.write(json.dumps(redact({
                    "iteration": run.iterations, "tool": outcome.name, "ok": outcome.ok,
                    "args": _truncate_args(fn.get("arguments")), "duration_s": round(time.monotonic() - t0, 3),
                    "output_chars": len(outcome.content),
                })) + "\n")
                tool_log.flush()
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": outcome.content})
                if outcome.finished:
                    run.finished = outcome.finished
                    break
    finally:
        tool_log.close()
        client.sink = original_sink
    return run, messages


def _is_transport(e: LLMError) -> bool:
    last = e.records[-1] if e.records else None
    return bool(last and (last.http_status is None or last.http_status >= 500 or last.http_status in (404, 429)))


def _truncate_args(raw: Any, limit: int = 600) -> Any:
    text = raw if isinstance(raw, str) else json.dumps(raw)
    return text if len(text) <= limit else text[:limit] + f"...[{len(text) - limit} more chars]"


def validate(sandbox: Sandbox, commands: list[str], timeout_s: float = 900) -> list[ValidationResult]:
    results = []
    for cmd in commands:
        r = sandbox.run(cmd, timeout_s=timeout_s, check_policy=False)
        results.append(ValidationResult(command=cmd, passed=r.ok, exit_code=r.exit_code,
                                        duration_s=r.duration_s, timed_out=r.timed_out,
                                        output_tail=r.output[-4000:]))
    return results


def execute(
    req: DispatchRequest,
    client: ChatClient,
    sandbox: Sandbox,
    generation: dict[str, Any],
    max_model_len: int,
    out_dir: Path,
    model_identity: dict[str, Any],
    dispatch_id: str | None = None,
) -> DispatchResult:
    """Full dispatch: snapshot -> agent loop -> validation -> diff -> result."""
    dispatch_id = dispatch_id or f"d-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = Path(req.repo_path).resolve()
    base = snapshot.snapshot(workspace)
    wall0 = time.monotonic()
    run, messages = run_agent(req, client, workspace, sandbox, generation, max_model_len, out_dir)
    agent_s = time.monotonic() - wall0
    validation = validate(sandbox, req.validation_commands, timeout_s=req.limits.command_timeout_s)
    head = snapshot.snapshot(workspace)
    changes = snapshot.changes(workspace, base, head)
    patch = snapshot.diff(workspace, base, head)
    (out_dir / "diff.patch").write_text(patch)
    (out_dir / "transcript.json").write_text(json.dumps(redact(messages), indent=1))

    all_passed = all(v.passed for v in validation)
    failure = run.failure
    if run.finished:
        st = run.finished["status"]
        if st == "blocked":
            failure = Failure(kind="agent_blocked", message=run.finished.get("notes") or run.finished["summary"])
        elif st == "failed":
            failure = Failure(kind="agent_gave_up", message=run.finished.get("notes") or run.finished["summary"])
        elif not all_passed:
            failed = [v.command for v in validation if not v.passed]
            failure = Failure(kind="validation_failed", message=f"agent reported completion but validation failed: {failed}")
    elif failure is None:
        failure = Failure(kind="internal", message="loop ended without finish or failure")

    status = "completed" if failure is None else ("blocked" if failure.kind == "agent_blocked" else "failed")
    if failure and failure.kind in ("endpoint_unavailable", "llm_error") and not changes:
        status = "error"
    metrics = summarize_records(run.records)
    metrics.update({
        "iterations": run.iterations,
        "tool_calls": run.tool_calls,
        "agent_wall_s": round(agent_s, 3),
        "validation_s": round(sum(v.duration_s for v in validation), 3),
    })
    served = sorted({r.served_model for r in run.records if r.served_model})
    return DispatchResult(
        dispatch_id=dispatch_id,
        status=status,
        summary=(run.finished or {}).get("summary", "") if run.finished else "",
        agent_notes=(run.finished or {}).get("notes") if run.finished else None,
        role=req.role,
        files_changed=changes,
        lines_added=sum(c.added for c in changes),
        lines_deleted=sum(c.deleted for c in changes),
        validation=validation,
        metrics=metrics,
        model={**model_identity, "served_models_observed": served},
        failure=failure,
        base_tree=base,
        result_tree=head,
        artifacts={"diff": str(out_dir / "diff.patch"), "transcript": str(out_dir / "transcript.json"),
                   "tools": str(out_dir / "tools.jsonl")},
    )
