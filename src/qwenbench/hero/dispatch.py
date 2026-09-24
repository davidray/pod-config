"""`qwenbench dispatch`: run one Qwen-assigned role on a project and return a DispatchResult.

Exit codes (Claude reads the JSON; codes make shell use unambiguous):
  0 completed   1 failed/blocked   3 refused by policy (role is not Qwen-routed)
  4 endpoint unavailable            5 attempt budget for this task exhausted
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from qwenbench.agent.loop import execute
from qwenbench.agent.protocol import DispatchRequest, DispatchResult, Failure
from qwenbench.agent.sandbox import Sandbox
from qwenbench.agent.snapshot import file_blob
from qwenbench.config import Config
from qwenbench.hero.heroconfig import effective_models
from qwenbench.hero.ledger import attempts_for, load_binding, log_dispatch, routing_dir, task_key
from qwenbench.hero.policy import resolve
from qwenbench.metrics.llm import ChatClient, JsonlSink
from qwenbench.paths import state_dir
from qwenbench.providers.base import first_ready
from qwenbench.providers.openai_compat import OpenAICompatibleModel

EXIT = {"completed": 0, "failed": 1, "blocked": 1, "error": 4}


def _refusal(req: DispatchRequest, dispatch_id: str, kind: str, message: str, retryable: bool = False) -> DispatchResult:
    return DispatchResult(dispatch_id=dispatch_id, status="error", role=req.role,
                          failure=Failure(kind=kind, message=message, retryable=retryable))


def run_dispatch(cfg: Config, req: DispatchRequest, project: Path, provider_factory=None,
                 client_factory=None) -> tuple[DispatchResult, int]:
    project = project.resolve()
    dispatch_id = f"d-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    if Path(req.repo_path).resolve() != project:
        return _refusal(req, dispatch_id, "policy",
                        f"repo_path {req.repo_path} is not the project {project}"), 3
    route = resolve(cfg.policy, req.role, effective_models(project))
    if not route.is_qwen:
        msg = (f"role {req.role!r} is not routed to Qwen ({route.reason}). "
               + ("Claude performs this role natively." if route.is_frontier else "It is denied by policy."))
        return _refusal(req, dispatch_id, "policy", msg), 3
    pinned = req.profile or route.profile

    key = task_key(req.role, req.task, req.spec_ref)
    prior = attempts_for(project, key)
    budget = cfg.policy.enforcement.dispatch.max_attempts_per_task
    if prior >= budget:
        msg = (f"this task has already been dispatched {prior} time(s) (max_attempts_per_task={budget}). "
               "Stop and surface the failure to the human. Claude must not implement it instead; only a human "
               "override (`qwenbench override grant`) or a revised task/spec can continue.")
        res = _refusal(req, dispatch_id, "policy", msg)
        log_dispatch(project, event="dispatch-refused", dispatch_id=dispatch_id, task_key=key, reason=msg,
                     role=req.role)
        return res, 5

    if provider_factory is None:
        from qwenbench.runpod.provider import RunpodPodsProvider
        provider = RunpodPodsProvider(cfg)
    else:
        provider = provider_factory(cfg)
    endpoint = first_ready(provider, cfg, [pinned] if pinned else cfg.preferred_profiles())
    if endpoint is None:
        wanted = f"profile {pinned!r}" if pinned else f"any of {', '.join(cfg.preferred_profiles())}"
        msg = (f"no ready endpoint for {wanted}. A human should run `qwenbench up{' ' + pinned if pinned else ''}` "
               "(or check `qwenbench status`). Do not fall back to implementing this with Claude.")
        log_dispatch(project, event="dispatch-unavailable", dispatch_id=dispatch_id, role=req.role, profile=pinned)
        return _refusal(req, dispatch_id, "endpoint_unavailable", msg, retryable=True), 4
    profile_name = endpoint.profile
    profile = cfg.profile(profile_name)
    health = OpenAICompatibleModel(endpoint).health()
    if not (health["health_ok"] and health["model_listed"]):
        msg = f"endpoint for {profile_name} is not healthy ({health.get('error') or health}); run `qwenbench status {profile_name}`."
        log_dispatch(project, event="dispatch-unavailable", dispatch_id=dispatch_id, role=req.role, profile=profile_name,
                     health=health)
        return _refusal(req, dispatch_id, "endpoint_unavailable", msg, retryable=True), 4

    out_dir = routing_dir(project) / "dispatches" / dispatch_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "request.json").write_text(req.model_dump_json(indent=2))
    binding = load_binding(project) or {}
    sb_cfg = binding.get("sandbox") or {}
    scratch = state_dir() / "dispatch-scratch" / dispatch_id
    sandbox = Sandbox(project, scratch, network=req.allow_network or bool(sb_cfg.get("network")),
                      allow_unsandboxed=bool(sb_cfg.get("allow_unsandboxed")),
                      extra_writable=[Path(p).expanduser() for p in sb_cfg.get("extra_writable", [])],
                      extra_env={"HOME": str(Path.home())} if sb_cfg.get("real_home") else {},
                      protected=protected_paths(cfg, project))
    sink = JsonlSink(out_dir / "requests.jsonl", extra_secrets=[endpoint.api_key])
    ctx = {"dispatch_id": dispatch_id, "role": req.role, "profile": profile_name, "task_key": key}
    client = (client_factory or ChatClient)(endpoint.base_url, endpoint.api_key, endpoint.model, sink=sink,
                                            max_retries=cfg.policy.enforcement.dispatch.max_request_retries,
                                            context=ctx)
    identity = {
        "provider": "qwen", "route": route.to_dict(), "profile": profile_name, "endpoint_model": endpoint.model,
        "hf_repo": profile.model.hf_repo, "revision": profile.model.revision, **endpoint.metadata,
    }
    log_dispatch(project, event="dispatch-start", dispatch_id=dispatch_id, role=req.role, task_key=key,
                 attempt=prior + 1, spec_ref=req.spec_ref, model=identity)
    try:
        result = execute(req, client, sandbox, profile.model.generation.model_dump(),
                         profile.runtime.max_model_len, out_dir, identity, dispatch_id=dispatch_id)
    finally:
        client.close()
    result.artifacts["requests"] = str(out_dir / "requests.jsonl")
    (out_dir / "result.json").write_text(result.model_dump_json(indent=2))
    files = [{"path": c.path, "status": c.status,
              "blob": file_blob(project, result.result_tree, c.path) if result.result_tree else None}
             for c in result.files_changed]
    log_dispatch(project, event="dispatch-end", dispatch_id=dispatch_id, role=req.role, task_key=key,
                 attempt=prior + 1, status=result.status, model=result.model, files=files,
                 failure=result.failure.model_dump() if result.failure else None, metrics=result.metrics)
    code = EXIT[result.status]
    if result.status != "completed" and prior + 1 >= budget:
        result.failure = result.failure or Failure(kind="internal", message="unknown")
        result.failure.message += (" | attempt budget exhausted: stop and surface this to the human; "
                                   "do not implement it with Claude.")
    return result, code


def protected_paths(cfg: Config, project: Path) -> list[Path]:
    """Concrete paths for the policy's protected globs (plus the routing dir itself)."""
    paths = {project / ".qwen-routing"}
    for g in cfg.policy.enforcement.protected:
        base = g.split("*", 1)[0].rstrip("/")
        if base:
            paths.add(project / base)
    return sorted(paths)


def build_request(args: dict[str, Any]) -> DispatchRequest:
    if args.get("request"):
        data = json.loads(Path(args["request"]).read_text())
        return DispatchRequest(**data)
    task = args.get("task") or ""
    if args.get("task_file"):
        task = Path(args["task_file"]).read_text()
    if not task.strip():
        raise ValueError("a task is required (--task, --task-file or --request)")
    return DispatchRequest(
        role=args["role"], task=task, repo_path=str(Path(args["project"]).resolve()),
        context=Path(args["context_file"]).read_text() if args.get("context_file") else (args.get("context") or ""),
        acceptance_criteria=list(args.get("criteria") or []),
        validation_commands=list(args.get("validate") or []),
        spec_ref=args.get("spec"), profile=args.get("profile"), allow_network=bool(args.get("allow_network")),
    )
