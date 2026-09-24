"""Lifecycle commands: up, down, status, list, endpoint, logs, chat, keepalive, guard, infra, storage."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import typer
from rich.table import Table

from qwenbench.cli.common import cfg, console, err, handle_errors, provider
from qwenbench.config import format_duration, parse_duration
from qwenbench.metrics.llm import ChatClient, LLMError
from qwenbench.runpod import guard as guardmod
from qwenbench.runpod import podspec
from qwenbench.runpod.provider import UpOptions
from qwenbench.state import all_sessions, load_session, log_event, read_events


def _opt_duration(value: str | None) -> float | None | str:
    if value is None:
        return "default"
    return parse_duration(value)


def _opt_spend(value: str | None) -> float | None | str:
    if value is None:
        return "default"
    return None if value.strip().lower() in ("off", "none") else float(value)


@handle_errors
def up(
    profile: str = typer.Argument(None, help="Profile from config/runpod.yaml, e.g. a6000 or l40s. "
                                            "Omit to use the first of profile_preference with capacity."),
    idle_timeout: str = typer.Option(None, help="e.g. 60m; 'off' disables (explicit, and warned about)"),
    max_session: str = typer.Option(None, help="Hard lifetime cap, e.g. 4h; 'off' disables"),
    max_spend: str = typer.Option(None, help="USD cap for this session; 'off' disables"),
    startup_timeout: str = typer.Option(None, help="Fail and terminate if not ready within this, e.g. 30m"),
    storage: str = typer.Option(None, help="network-volume (default) or ephemeral (re-download weights)"),
    allow_concurrent: bool = typer.Option(False, help="Allow another qwenbench pod to be live (still capped)"),
    keep_on_failure: bool = typer.Option(False, help="Leave a failed pod running for debugging (billing!)"),
    no_guard: bool = typer.Option(False, help="Do not start the local backstop guard process"),
) -> None:
    """Provision a pod, wait until the model actually serves, and print the endpoint."""
    c = cfg()
    candidates = [c.profile(n) for n in ([profile] if profile else c.preferred_profiles())]
    if storage:
        candidates = [p.model_copy(update={"storage": p.storage.model_copy(update={"mode": storage})})
                      for p in candidates]
    opts = UpOptions(idle_timeout_s=_opt_duration(idle_timeout), max_session_s=_opt_duration(max_session),
                     max_spend_usd=_opt_spend(max_spend),
                     startup_timeout_s=parse_duration(startup_timeout) if startup_timeout else None,
                     allow_concurrent=allow_concurrent, keep_on_failure=keep_on_failure)
    if opts.idle_timeout_s is None:
        err.print("[yellow]WARNING: idle shutdown DISABLED for this session. The pod bills until `qwenbench down` "
                  "or max-session/max-spend trips.[/]")
    for p in candidates:
        rate = c.pricing.gpu_rate(p.gpu_type_id, p.cloud) * p.gpu_count
        console.print(f"[bold]{p.name}[/]: {p.gpu_type_id} ({p.cloud}) list price ~${rate:.2f}/hr; "
                      f"model {p.model.hf_repo}@{p.model.revision[:10]}")
    prov = provider(c)
    t0 = time.time()
    ep = prov.up_first_available(candidates, opts, progress=lambda m: console.print(m))
    profile = ep.profile
    session = load_session(profile)
    console.print(f"[green]ready[/] in {format_duration(time.time() - t0)}: {ep.base_url}  model={ep.model}")
    if session:
        lim = session.limits
        console.print(f"guards: idle {format_duration(lim.get('idle_timeout_s'))}, max session "
                      f"{format_duration(lim.get('max_session_s'))}, max spend "
                      f"{'off' if lim.get('max_spend_usd') is None else '$' + format(lim['max_spend_usd'], '.2f')}")
    if not no_guard:
        pid = guardmod.spawn_guard(profile)
        console.print(f"local guard running (pid {pid}); log: {guardmod.guard_pid_path(profile).with_suffix('.log')}")
    console.print(f"next: [bold]qwenbench chat {profile}[/] | [bold]qwenbench bench run --profile {profile} --suite daveeval[/]"
                  f" | [bold]qwenbench down {profile}[/]")


@handle_errors
def down(
    profile: str = typer.Argument(None, help="Profile to terminate"),
    all_: bool = typer.Option(False, "--all", help="Terminate every qwenbench pod on the account"),
) -> None:
    """Terminate pods (idempotent). Network volumes are kept; see `qwenbench storage`."""
    c = cfg()
    prov = provider(c)
    if all_:
        ids = prov.down_all()
    elif profile:
        ids = prov.down(c.profile(profile))
    else:
        err.print("specify a profile or --all")
        raise typer.Exit(2)
    console.print(f"terminated: {', '.join(ids)}" if ids else "nothing to terminate (already down)")
    remaining = [p for p in prov.our_pods() if p.is_live]
    if remaining:
        err.print(f"[yellow]still live: {', '.join(f'{p.name} {p.id} {p.status}' for p in remaining)}[/]")
        raise typer.Exit(1)


@handle_errors
def status(profile: str = typer.Argument(None)) -> None:
    """Live pods, spend so far, idle clock, guard state."""
    c = cfg()
    prov = provider(c)
    names = [profile] if profile else c.profile_names()
    t = Table(title="qwenbench endpoints")
    for col in ("profile", "state", "pod", "$/hr", "uptime", "spend (est)", "phase", "idle", "guard", "endpoint"):
        t.add_column(col)
    live_total = 0.0
    for n in names:
        st = prov.status(c.profile(n))
        sup = (st.detail or {}).get("supervisor") or {}
        g = guardmod.guard_running(n)
        if st.live:
            live_total += st.estimated_spend_usd or 0
        t.add_row(n, ("[red]" if st.live else "") + st.state, (st.detail or {}).get("pod_id", "-"),
                  f"{st.cost_per_hr:.2f}" if st.cost_per_hr else "-", format_duration(st.uptime_s) if st.uptime_s else "-",
                  f"${st.estimated_spend_usd:.2f}" if st.estimated_spend_usd else "-", sup.get("phase", "-"),
                  format_duration(sup.get("idle_for_s")) if sup.get("idle_for_s") is not None else "-",
                  f"pid {g}" if g else ("[yellow]none[/]" if st.live else "-"), (st.detail or {}).get("endpoint", "-"))
    console.print(t)
    strays = [p for p in prov.our_pods() if p.is_live and p.name.removeprefix(c.runpod.api.pod_name_prefix) not in names]
    for p in strays:
        err.print(f"[yellow]WARNING: live pod {p.name} ({p.id}) not matching any profile; `qwenbench down --all`[/]")
    if live_total or strays:
        err.print(f"[yellow]GPU pods are billing. Estimated spend so far: ${live_total:.2f}. `qwenbench down --all` stops "
                  "everything.[/]")
    shutdowns = read_events(5, kinds={"auto-shutdown"})
    if shutdowns:
        console.print("recent automatic shutdowns:")
        for e in shutdowns:
            console.print(f"  {e['ts']} {e.get('profile')} {e.get('reason')}: {e.get('detail')} ({e.get('source')})")


@handle_errors
def list_() -> None:
    """Profiles, their GPUs and live pods."""
    c = cfg()
    t = Table(title="profiles (config/runpod.yaml)")
    for col in ("profile", "gpu", "cloud", "list $/hr", "data centers", "storage", "idle timeout"):
        t.add_column(col)
    for n in c.profile_names():
        p = c.profile(n)
        t.add_row(n, p.gpu_type_id, p.cloud, f"{c.pricing.gpu_rate(p.gpu_type_id, p.cloud):.2f}",
                  ", ".join(p.data_center_ids), f"{p.storage.mode}:{p.storage.volume_name or '-'}",
                  format_duration(p.idle_timeout_s))
    console.print(t)
    try:
        pods = provider(c).our_pods()
    except Exception as e:  # listing profiles must work without credentials
        err.print(f"(live pods unavailable: {e})")
        return
    for p in pods:
        console.print(f"pod {p.name} {p.id} {p.status} ${p.cost_per_hr:.2f}/hr {p.data_center_id}")
    if not pods:
        console.print("no qwenbench pods exist")


@handle_errors
def endpoint(profile: str, export: bool = typer.Option(False, help="Print shell exports"),
             show_key: bool = typer.Option(False, help="Include the bearer token")) -> None:
    """Print the OpenAI-compatible endpoint for a ready profile."""
    ep = provider().endpoint(cfg().profile(profile))
    if not ep:
        err.print(f"{profile} is not up. `qwenbench up {profile}`")
        raise typer.Exit(1)
    if export:
        console.print(f"export OPENAI_BASE_URL={ep.base_url}\nexport OPENAI_MODEL={ep.model}")
        console.print(f"export OPENAI_API_KEY={ep.api_key if show_key else '<qwenbench endpoint --show-key>'}")
        return
    console.print_json(json.dumps({"base_url": ep.base_url, "model": ep.model,
                                   "api_key": ep.api_key if show_key else "(hidden; --show-key)", **ep.metadata}))


@handle_errors
def logs(profile: str, tail: int = typer.Option(200), follow: bool = typer.Option(False, "--follow", "-f")) -> None:
    """Container logs via the Runpod logs API (falls back to the in-pod supervisor)."""
    for line in provider().logs(cfg().profile(profile), tail=tail, follow=follow):
        console.print(line, markup=False, highlight=False)


@handle_errors
def chat(profile: str, once: str = typer.Option(None, help="Send one prompt and exit"),
         system: str = typer.Option("You are a helpful senior software engineer.")) -> None:
    """Interactive chat with per-response latency/throughput."""
    c = cfg()
    p = c.profile(profile)
    ep = provider(c).endpoint(p)
    if not ep:
        err.print(f"{profile} is not up. `qwenbench up {profile}`")
        raise typer.Exit(1)
    client = ChatClient(ep.base_url, ep.api_key, ep.model, context={"kind": "chat", "profile": profile})
    messages = [{"role": "system", "content": system}]
    params = p.model.generation.model_dump()
    while True:
        prompt = once if once else typer.prompt("you", prompt_suffix="> ", default="", show_default=False)
        if not prompt or prompt.strip() in ("/exit", "/quit"):
            break
        messages.append({"role": "user", "content": prompt})
        try:
            r = client.chat(messages, params=params)
        except LLMError as e:
            err.print(f"[red]{e}[/]")
            if once:
                raise typer.Exit(1) from e
            continue
        messages.append({"role": "assistant", "content": r.content})
        console.print(r.content, markup=False)
        rec = r.record
        console.print(f"[dim]ttft {rec.ttft_s}s | {rec.output_tokens} tok @ {rec.output_tokens_per_s} tok/s | "
                      f"in {rec.input_tokens} (cached {rec.cached_input_tokens}) | total {rec.total_s}s | "
                      f"model {r.served_model}[/]")
        if once:
            break
    client.close()


@handle_errors
def keepalive(profile: str) -> None:
    """Reset the in-pod idle clock without sending inference."""
    s = load_session(profile)
    if not s:
        err.print(f"{profile} is not up")
        raise typer.Exit(1)
    r = httpx.post(f"{s.watchdog_url}/touch", headers={"Authorization": f"Bearer {s.api_key}"}, timeout=15)
    console.print("idle clock reset" if r.status_code == 200 else f"failed: HTTP {r.status_code}")


def guard(profile: str, poll: float = typer.Option(60.0)) -> None:
    """(internal) Local backstop loop started by `qwenbench up`."""
    c = cfg()
    reason = guardmod.run_guard(provider(c), c.profile(profile), poll_s=poll)
    log_event("guard-exit", profile=profile, reason=reason)


def hook(kind: str = typer.Argument(..., help="pre-tool-use | session-start | stop")) -> None:
    """(internal) Claude Code hook entry point."""
    from qwenbench.hero.hooks import main as hook_main

    raise typer.Exit(hook_main(kind))


infra_app = typer.Typer(help="Infrastructure definition (rendered pod specs)", no_args_is_help=True)


def _rendered_dir() -> Path:
    from qwenbench.paths import repo_root

    return repo_root() / "infra" / "runpod" / "rendered"


def render_profile(c, name: str) -> dict:
    p = c.profile(name)
    env = podspec.pod_env(p, endpoint_api_key="<generated per session>",
                          list_cost_per_hr=c.pricing.gpu_rate(p.gpu_type_id, p.cloud) * p.gpu_count)
    body = podspec.create_body(p, env=env, data_center_ids=p.data_center_ids[:1],
                               network_volume_id="<discovered>" if p.storage.mode == "network-volume" else None)
    return {"profile": name, "note": "Generated by `qwenbench infra render`; do not edit. Source: config/*.yaml",
            "vllm_command": " ".join(podspec.vllm_argv(p)), "create_pod_request": podspec.rendered_for_repo(body)}


@infra_app.command("render")
def infra_render(check: bool = typer.Option(False, help="Fail if committed files are stale")) -> None:
    """Write infra/runpod/rendered/<profile>.json from config (the committed IaC)."""
    c = cfg()
    out = _rendered_dir()
    out.mkdir(parents=True, exist_ok=True)
    stale = []
    for name in c.profile_names():
        text = json.dumps(render_profile(c, name), indent=2) + "\n"
        path = out / f"{name}.json"
        if check:
            if not path.exists() or path.read_text() != text:
                stale.append(path.name)
        else:
            path.write_text(text)
            console.print(f"wrote {path}")
    if check:
        if stale:
            err.print(f"stale rendered infra: {', '.join(stale)}; run `qwenbench infra render`")
            raise typer.Exit(1)
        console.print("rendered infra is up to date")


@infra_app.command("show")
def infra_show(profile: str, image: bool = typer.Option(False), argv: bool = typer.Option(False)) -> None:
    """Show the rendered spec (or just --image / --argv) for a profile."""
    c = cfg()
    if image:
        console.print(c.profile(profile).model.image)
    elif argv:
        console.print(" ".join(podspec.vllm_argv(c.profile(profile))))
    else:
        console.print_json(json.dumps(render_profile(c, profile)))


storage_app = typer.Typer(help="Persistent model-cache network volumes", no_args_is_help=True)


@storage_app.command("list")
@handle_errors
def storage_list() -> None:
    """Network volumes and their standing monthly cost."""
    c = cfg()
    per_gb = c.pricing.storage_monthly_per_gb.get("network_volume", 0)
    names = {c.profile(n).storage.volume_name: n for n in c.profile_names()}
    for v in provider(c).client.list_network_volumes():
        tag = f"(profile {names[v.name]})" if v.name in names else ""
        console.print(f"{v.name} {v.id} {v.size_gb} GB in {v.data_center} ~${v.size_gb * per_gb:.2f}/month {tag}")


@storage_app.command("ensure")
@handle_errors
def storage_ensure(profile: str) -> None:
    """Create the profile's cache volume if missing (normally done by `qwenbench up`)."""
    c = cfg()
    v = provider(c).ensure_volume(c.profile(profile), progress=console.print)
    console.print(f"{v.name} {v.id} in {v.data_center}" if v else "profile uses ephemeral storage")


@storage_app.command("delete")
@handle_errors
def storage_delete(profile: str, yes: bool = typer.Option(False, "--yes")) -> None:
    """Delete the profile's cache volume (stops its monthly charge; next `up` re-downloads ~31 GB)."""
    c = cfg()
    prov = provider(c)
    v = prov.find_volume(c.profile(profile))
    if not v:
        console.print("no volume")
        return
    if not yes and not typer.confirm(f"delete {v.name} ({v.id}, {v.size_gb} GB)? cached weights will be lost"):
        raise typer.Exit(1)
    prov.client.delete_network_volume(v.id)
    log_event("volume-deleted", profile=profile, volume_id=v.id)
    console.print("deleted")


def sessions_summary() -> list[dict]:
    return [s.public_dict() for s in all_sessions()]
