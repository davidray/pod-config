"""Benchmark execution: suite x trials x cases against one endpoint."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qwenbench import __version__
from qwenbench.agent import snapshot
from qwenbench.agent.loop import run_agent, summarize_records, validate
from qwenbench.agent.protocol import DispatchRequest, Limits
from qwenbench.agent.sandbox import Sandbox, detect_backend
from qwenbench.bench import workspace as wsmod
from qwenbench.bench.suite import Case, Suite
from qwenbench.config import Config, Profile
from qwenbench.metrics.llm import ChatClient, JsonlSink
from qwenbench.paths import repo_root, results_dir
from qwenbench.providers.base import Endpoint
from qwenbench.runpod import podspec
from qwenbench.secrets import redact

Progress = Callable[[str], None]


@dataclass
class Intervention:
    kind: str            # "hint" | "manual-fix" | "skip" | "setup-fix"
    reason: str
    at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


# on_failure(case, trial, attempt_result, workspace_path) -> Intervention | None
FailureHook = Callable[[Case, int, dict[str, Any], Path], Intervention | None]


def utc(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def bench_repo_revision() -> dict[str, Any]:
    root = repo_root()
    try:
        head = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=root, capture_output=True,
                              text=True)
        sha = head.stdout.strip() if head.returncode == 0 else ""
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "src", "config", "benchmarks", "containers"],
                                    cwd=root, capture_output=True, text=True).stdout.strip())
    except OSError:
        sha, dirty = "", True
    return {"git_sha": sha or None, "dirty": dirty, "qwenbench_version": __version__}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def reset_to_tree(ws: wsmod.Workspace, tree: str) -> None:
    """Undo validation-time changes (restores/overlays) back to the agent's result tree."""
    now = snapshot.snapshot(ws.repo)
    if now == tree:
        return
    for line in subprocess.run(["git", "diff", "--name-status", "--no-renames", tree, now], cwd=ws.repo,
                               capture_output=True, text=True).stdout.splitlines():
        code, path = line.split("\t", 1)
        if code == "A":
            (ws.repo / path).unlink(missing_ok=True)
        else:
            subprocess.run(["git", "checkout", tree, "--", path], cwd=ws.repo, capture_output=True)


def run_setup(case: Case, ws: wsmod.Workspace, log_path: Path) -> tuple[bool, float]:
    """Setup commands are trusted repo config (like a Makefile): run with network,
    without the agent sandbox, but with a scrubbed environment."""
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "RUNPOD", "ANTHROPIC"))}
    t0 = time.monotonic()
    with log_path.open("w") as log:
        for cmd in case.setup:
            log.write(f"$ {cmd}\n")
            log.flush()
            p = subprocess.run(["/bin/bash", "-c", cmd], cwd=ws.repo, env=env, stdout=log, stderr=subprocess.STDOUT,
                               timeout=1200)
            if p.returncode != 0:
                log.write(f"[exit {p.returncode}]\n")
                return False, time.monotonic() - t0
    return True, time.monotonic() - t0


def validate_case(case: Case, ws: wsmod.Workspace, sandbox: Sandbox, result_tree: str) -> list[dict[str, Any]]:
    wsmod.restore_paths(ws, case.validation.restore)
    overlay = case.resolve(case.validation.overlay)
    if overlay:
        wsmod.apply_overlay(ws, overlay)
    try:
        res = validate(sandbox, case.validation.commands, timeout_s=case.validation_timeout_s)
    finally:
        reset_to_tree(ws, result_tree)
    return [r.model_dump() for r in res]


def run_metadata(cfg: Config, profile: Profile, endpoint: Endpoint, suite: Suite, cases: list[Case],
                 repeat: int, max_attempts: int, run_id: str, session_public: dict[str, Any] | None) -> dict[str, Any]:
    m = profile.model
    return {
        "run_id": run_id,
        "schema": "qwenbench.run/v1",
        "suite": suite.name,
        "repeat": repeat,
        "max_attempts": max_attempts,
        "profile": profile.name,
        "compute": {
            "provider": endpoint.metadata.get("compute"),
            "gpu_type_id": profile.gpu_type_id,
            "gpu_count": profile.gpu_count,
            "cloud": profile.cloud,
            "pod_id": endpoint.metadata.get("pod_id"),
            "data_center_id": endpoint.metadata.get("data_center_id"),
            "config_fingerprint": endpoint.metadata.get("config_fingerprint"),
        },
        "model": {
            "hf_repo": m.hf_repo, "revision": m.revision, "served_model_name": m.served_model_name,
            "quantization": m.quantization, "max_model_len": profile.runtime.max_model_len,
        },
        "runtime": {
            "image": m.image, "vllm_version": m.vllm_version, "cuda_version": m.cuda_version,
            "config": profile.runtime.model_dump(),
            "vllm_argv": podspec.vllm_argv(profile),
            "hardware_overrides": profile.runtime_overrides,   # non-empty = NOT identical across GPUs
        },
        "generation": m.generation.model_dump(),
        "agent": {"loop": "qwenbench.agent.loop", "sandbox": detect_backend(), **bench_repo_revision()},
        "cases": [],
        "session": session_public,
        "started_at": utc(),
    }


@dataclass
class RunContext:
    cfg: Config
    profile: Profile
    endpoint: Endpoint
    run_dir: Path
    sink: JsonlSink
    max_attempts: int
    allow_unsandboxed: bool = False
    keep_workspaces: bool = False
    on_failure: FailureHook | None = None
    progress: Progress = print
    client_factory: Callable[[Endpoint, JsonlSink, dict[str, Any]], ChatClient] | None = None
    cost_per_hr: float | None = None


def make_client(ep: Endpoint, sink: JsonlSink, context: dict[str, Any], retries: int = 3) -> ChatClient:
    return ChatClient(ep.base_url, ep.api_key, ep.model, sink=sink, max_retries=retries, context=context)


def run_case_trial(ctx: RunContext, case: Case, trial: int) -> dict[str, Any]:
    out = ctx.run_dir / "tasks" / case.id / f"trial-{trial}"
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    t0 = time.monotonic()
    ws = wsmod.create(case)
    result: dict[str, Any] = {
        "case_id": case.id, "title": case.title, "category": case.category, "trial": trial, "role": case.task.role,
        "profile": ctx.profile.name, "starting_commit": ws.starting_commit, "source": ws.source_desc,
        "started_at": utc(started), "attempts": [], "interventions": [], "success": False, "first_pass": False,
    }
    try:
        ok, setup_s = run_setup(case, ws, out / "setup.log")
        result["setup_s"] = round(setup_s, 2)
        if not ok:
            result["failure"] = {"kind": "setup_failed", "message": f"setup failed; see {out / 'setup.log'}"}
            return result
        sandbox = Sandbox(ws.repo, ws.scratch, network=False, allow_unsandboxed=ctx.allow_unsandboxed)
        result["sandbox"] = sandbox.backend
        base_tree = snapshot.snapshot(ws.repo)
        feedback = ""
        context_ids = {"run_id": ctx.run_dir.name, "task_id": case.id, "trial": trial, "profile": ctx.profile.name}
        agent_t0 = time.monotonic()
        attempt = 0
        extra_attempts = 0  # granted by human hints; recorded as interventions
        while attempt < ctx.max_attempts + extra_attempts:
            attempt += 1
            attempt_dir = out / f"attempt-{attempt}"
            attempt_dir.mkdir(exist_ok=True)
            factory = ctx.client_factory or make_client
            client = factory(ctx.endpoint, ctx.sink, {**context_ids, "attempt": attempt})
            remaining = max(60.0, case.timeout_s - (time.monotonic() - agent_t0))
            req = DispatchRequest(
                role=case.task.role, task=case.prompt, repo_path=str(ws.repo),
                context=case.context + (f"\n\n## Feedback from the previous attempt\n{feedback}" if feedback else ""),
                acceptance_criteria=case.task.acceptance_criteria, validation_commands=case.agent_validation,
                limits=Limits(max_iterations=case.limits.max_iterations, timeout_s=remaining,
                              max_total_output_tokens=case.limits.max_total_output_tokens),
            )
            ctx.progress(f"  {case.id} trial {trial} attempt {attempt}: agent running")
            run, messages = run_agent(req, client, ws.repo, sandbox, ctx.profile.model.generation.model_dump(),
                                      ctx.profile.runtime.max_model_len, attempt_dir)
            client.close()
            (attempt_dir / "transcript.json").write_text(json.dumps(redact(messages), indent=1))
            result_tree = snapshot.snapshot(ws.repo)
            ctx.progress(f"  {case.id} trial {trial} attempt {attempt}: validating")
            validation = validate_case(case, ws, sandbox, result_tree)
            passed = all(v["passed"] for v in validation)
            att = {
                "attempt": attempt,
                "finish": run.finished,
                "failure": run.failure.model_dump() if run.failure else None,
                "validation": validation,
                "passed": passed,
                "iterations": run.iterations,
                "tool_calls": run.tool_calls,
                **summarize_records(run.records),
            }
            result["attempts"].append(att)
            (attempt_dir / "validation.log").write_text("\n\n".join(
                f"$ {v['command']}\n[passed={v['passed']} exit={v['exit_code']}]\n{v['output_tail']}" for v in validation))
            if passed:
                result["success"] = True
                result["first_pass"] = attempt == 1 and not result["interventions"]
                break
            if run.failure and run.failure.kind in ("endpoint_unavailable", "llm_error"):
                result["failure"] = run.failure.model_dump()
                break
            failed = [v for v in validation if not v["passed"]]
            feedback = "Validation failed after your previous attempt. Your changes are still in the repository.\n" + \
                "\n".join(f"$ {v['command']} (exit {v['exit_code']})\n{v['output_tail'][-3000:]}" for v in failed)
            if attempt >= ctx.max_attempts + extra_attempts and ctx.on_failure:
                iv = ctx.on_failure(case, trial, att, ws.repo)
                if iv and iv.kind != "skip":
                    result["interventions"].append(iv.__dict__)
                    if iv.kind == "hint":
                        feedback += f"\n\nHint from the human reviewer: {iv.reason}"
                        extra_attempts += 1
                        continue
                    if iv.kind == "manual-fix":
                        tree = snapshot.snapshot(ws.repo)
                        validation = validate_case(case, ws, sandbox, tree)
                        att["validation_after_manual_fix"] = validation
                        result["success"] = all(v["passed"] for v in validation)
                elif iv:
                    result["interventions"].append(iv.__dict__)
        final_tree = snapshot.snapshot(ws.repo)
        changes = snapshot.changes(ws.repo, base_tree, final_tree)
        (out / "diff.patch").write_text(snapshot.diff(ws.repo, base_tree, final_tree))
        result.update({
            "files_changed": [c.model_dump() for c in changes],
            "lines_added": sum(c.added for c in changes),
            "lines_deleted": sum(c.deleted for c in changes),
            "base_tree": base_tree, "result_tree": final_tree,
        })
        if not result["success"] and "failure" not in result:
            last = result["attempts"][-1] if result["attempts"] else {}
            result["failure"] = last.get("failure") or {"kind": "validation_failed",
                                                         "message": "validation did not pass"}
    finally:
        wall = time.monotonic() - t0
        result["ended_at"] = utc()
        result["wall_s"] = round(wall, 2)
        attempts = result["attempts"]
        for key in ("requests", "failed_requests", "retries", "input_tokens", "output_tokens",
                    "cached_input_tokens", "iterations", "tool_calls"):
            result[key] = sum(a.get(key) or 0 for a in attempts)
        result["inference_s"] = round(sum(a.get("inference_s") or 0 for a in attempts), 2)
        ttfts = [a["median_ttft_s"] for a in attempts if a.get("median_ttft_s")]
        tps = [a["median_output_tps"] for a in attempts if a.get("median_output_tps")]
        result["median_ttft_s"] = sorted(ttfts)[len(ttfts) // 2] if ttfts else None
        result["median_output_tps"] = sorted(tps)[len(tps) // 2] if tps else None
        result["manual_intervention"] = any(i["kind"] != "skip" for i in result["interventions"])
        result["intervention_reasons"] = [i["reason"] for i in result["interventions"]]
        # The GPU is dedicated to the run, so task wall time is its GPU-attributable time.
        result["gpu_attributable_s"] = round(wall, 2)
        if ctx.cost_per_hr:
            result["estimated_compute_cost_usd"] = round(ctx.cost_per_hr * wall / 3600, 4)
        (out / "result.json").write_text(json.dumps(redact(result), indent=2))
        if ctx.keep_workspaces:
            result["workspace"] = str(ws.repo)
        else:
            ws.cleanup()
    return result


def run_benchmark(
    cfg: Config, profile: Profile, endpoint: Endpoint, suite: Suite, cases: list[Case], *,
    repeat: int = 1, max_attempts: int | None = None, session_public: dict[str, Any] | None = None,
    cost_per_hr: float | None = None, allow_unsandboxed: bool = False, keep_workspaces: bool = False,
    on_failure: FailureHook | None = None, progress: Progress = print, client_factory=None,
    run_id: str | None = None,
) -> Path:
    max_attempts = max_attempts or suite.defaults.max_attempts
    run_id = run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{profile.name}-{suite.name}"
    run_dir = results_dir() / run_id
    run_dir.mkdir(parents=True)
    meta = run_metadata(cfg, profile, endpoint, suite, cases, repeat, max_attempts, run_id, session_public)
    meta["cost_per_hr"] = cost_per_hr
    for c in cases:
        meta["cases"].append({
            "id": c.id, "category": c.category, "role": c.task.role,
            "prompt_sha256": sha256_text(c.prompt + c.context),
            "agent_validation": c.agent_validation, "validation": c.validation.model_dump(),
            "limits": c.limits.model_dump(),
        })
    write_json(run_dir / "run.json", meta)
    sink = JsonlSink(run_dir / "requests.jsonl", extra_secrets=[endpoint.api_key])
    ctx = RunContext(cfg, profile, endpoint, run_dir, sink, max_attempts, allow_unsandboxed, keep_workspaces,
                     on_failure, progress, client_factory, cost_per_hr)
    t0 = time.time()
    results = []
    for trial in range(1, repeat + 1):
        for case in cases:
            progress(f"[trial {trial}/{repeat}] {case.id}")
            r = run_case_trial(ctx, case, trial)
            results.append(r)
            meta_case = next(m for m in meta["cases"] if m["id"] == case.id)
            meta_case.setdefault("starting_commits", {})[f"trial-{trial}"] = r.get("starting_commit")
            status = "PASS" if r["success"] else f"FAIL ({(r.get('failure') or {}).get('kind', '?')})"
            progress(f"  -> {status} in {r['wall_s']:.0f}s, {r['requests']} requests, "
                     f"{r['output_tokens']} output tokens")
    meta["ended_at"] = utc()
    meta["benchmark_wall_s"] = round(time.time() - t0, 2)
    write_json(run_dir / "run.json", meta)
    return run_dir


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(redact(data), indent=2))


def copy_reference(case: Case, ws: wsmod.Workspace) -> bool:
    """Apply benchmarks/tasks/<id>/reference.patch (for `qwen bench verify-cases`)."""
    patch = case.case_dir / "reference.patch"
    if not patch.exists():
        return False
    p = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=ws.repo, capture_output=True,
                       text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{case.id}: reference.patch does not apply: {p.stderr}")
    return True


def verify_case(case: Case, allow_unsandboxed: bool = False) -> dict[str, Any]:
    """Base state must FAIL validation; reference solution must PASS it."""
    out: dict[str, Any] = {"case": case.id}
    ws = wsmod.create(case)
    try:
        log = ws.scratch / "setup.log"
        ok, _ = run_setup(case, ws, log)
        if not ok:
            return {**out, "ok": False, "error": log.read_text()[-2000:]}
        sandbox = Sandbox(ws.repo, ws.scratch, allow_unsandboxed=allow_unsandboxed)
        base = snapshot.snapshot(ws.repo)
        base_val = validate_case(case, ws, sandbox, base)
        out["base_fails"] = not all(v["passed"] for v in base_val)
        out["agent_validation_base"] = [v.model_dump()["passed"] for v in validate(sandbox, case.agent_validation)]
        if not copy_reference(case, ws):
            return {**out, "ok": False, "error": "no reference.patch"}
        ref = snapshot.snapshot(ws.repo)
        ref_val = validate_case(case, ws, sandbox, ref)
        out["reference_passes"] = all(v["passed"] for v in ref_val)
        out["reference_failures"] = [v for v in ref_val if not v["passed"]]
        out["ok"] = out["base_fails"] and out["reference_passes"]
        out["starting_commit"] = ws.starting_commit
        return out
    finally:
        shutil.rmtree(ws.root, ignore_errors=True)
