"""`qwen hero ...`, `qwen dispatch`, `qwen override ...`"""

from __future__ import annotations

import getpass
import json
from pathlib import Path

import typer
from rich.table import Table

from qwenbench.cli.common import cfg, console, err, is_tty, provider
from qwenbench.config import parse_duration
from qwenbench.hero import configure as conf
from qwenbench.hero import override as ov
from qwenbench.hero.audit import audit_report
from qwenbench.hero.dispatch import build_request, run_dispatch
from qwenbench.providers.openai_compat import OpenAICompatibleModel

hero_app = typer.Typer(help="Hero / Claude Code model routing", no_args_is_help=True)
override_app = typer.Typer(help="Human-only overrides of the Qwen routing", no_args_is_help=True)


@hero_app.command("inspect")
def hero_inspect(project: Path = typer.Argument(Path("."))) -> None:
    """Show Hero version, model roles, installed agents and the resulting routes."""
    info = conf.inspect(cfg(), project)
    console.print(f"project: {info['project']}")
    console.print(f"hero: {info['hero_version'] or 'NOT INSTALLED'} | workspace: {info['hero_workspace']} | "
                  f"install targets: {info['agents']['install_targets']}")
    console.print(f"models.roles (hero.json): {info['models_base'].get('roles')}")
    console.print(f"models.roles (hero.local.json): {info['models_local'].get('roles')}")
    console.print(f"effective: {info['models_effective']}")
    t = Table(title="agent routes")
    for col in ("agent", "hero role", "model", "provider", "installed", "reason"):
        t.add_column(col)
    installed = set(info["agents"]["installed"])
    for r in info["routes"]:
        color = {"qwen": "cyan", "frontier": "green"}.get(r["provider"], "red")
        t.add_row(r["agent"], r["hero_role"] or "-", r["model_id"] or "-", f"[{color}]{r['provider']}[/]",
                  "yes" if r["agent"] in installed else "", r["reason"][:70])
    console.print(t)
    if info["unclassified_agents"]:
        err.print(f"[yellow]unclassified agents (denied until added to config/role-policy.yaml): "
                  f"{', '.join(info['unclassified_agents'])}[/]")
    console.print(f"routing configured: {info['routing_configured']} | hooks installed: {info['hooks_installed']}")


@hero_app.command("configure")
def hero_configure(
    project: Path = typer.Argument(Path(".")),
    execution_profile: str = typer.Option(..., "--execution-profile", help="Compute profile serving Qwen"),
    frontier_model: str = typer.Option(conf.DEFAULT_FRONTIER_MODEL, help="Model id for design/review roles"),
    dry_run: bool = typer.Option(False, help="Show the diff only"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without confirmation"),
    no_enforce: bool = typer.Option(False, help="Record routing but do not enforce with hooks"),
) -> None:
    """Map Hero roles (design/review -> Claude, execution -> Qwen) and install enforcement."""
    c = cfg()
    plan = conf.plan_configure(c, project, execution_profile, frontier_model, enforce=not no_enforce)
    for note in plan.notes:
        err.print(f"[yellow]{note}[/]")
    diff = plan.diff()
    if not diff:
        console.print("already configured; nothing to change")
    else:
        console.print(diff, markup=False, highlight=False)
    if dry_run or not diff:
        return
    if not yes and not typer.confirm("apply these changes?"):
        raise typer.Exit(1)
    for p in conf.apply(plan):
        console.print(f"wrote {p}")
    rc, out = conf.run(["hero", "models", "--check"], project.resolve())
    console.print(out, markup=False)
    if rc != 0:
        err.print("[red]hero models --check failed[/]")
        raise typer.Exit(1)
    console.print("next: [bold]qwen hero verify " + str(project) + "[/]")


@hero_app.command("verify")
def hero_verify(project: Path = typer.Argument(Path("."))) -> None:
    """Prove the routing is wired: Hero config, hooks, simulated decisions, a real hook call, endpoint."""
    c = cfg()

    def endpoint_check(profile: str):
        try:
            ep = provider(c).endpoint(c.profile(profile))
        except Exception as e:
            return False, str(e)
        if not ep:
            return False, f"{profile} is not up (dispatch will fail fast until `qwen up {profile}`)"
        h = OpenAICompatibleModel(ep).health()
        return bool(h["health_ok"] and h["model_listed"]), json.dumps(h["models"])[:200]

    checks = conf.verify(c, project, endpoint_check=endpoint_check)
    fails = 0
    for ch in checks:
        color = {"ok": "green", "warn": "yellow"}.get(ch["status"], "red")
        fails += ch["status"] == "FAIL"
        console.print(f"[{color}]{ch['status']:>4}[/] {ch['check']}  [dim]{ch['detail']}[/]")
    raise typer.Exit(1 if fails else 0)


@hero_app.command("audit")
def hero_audit(project: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """Which model did what: dispatches, Claude-side subagents, denials, overrides, unattributed changes."""
    rep = audit_report(project.resolve(), cfg().policy)
    if as_json:
        console.print_json(json.dumps(rep, default=str))
        return
    t = Table(title="Qwen dispatches (ledger)")
    for col in ("time", "dispatch", "role", "status", "served model", "gpu", "pod", "files"):
        t.add_column(col)
    for d in rep["dispatches"]:
        t.add_row(d["ts"], d["dispatch_id"] or "", d["role"] or "", d["status"] or "", ", ".join(d["model"] or []),
                  d["gpu"] or "", d["pod"] or "", ", ".join(d["files"])[:60])
    console.print(t)
    console.print(f"Claude-side subagents allowed: {dict(rep['frontier_subagents'])}")
    console.print(f"denied tool calls by rule: {dict(rep['denied'])}")
    console.print(f"overrides: {len(rep['overrides'])}")
    if rep["unattributed_changes"]:
        err.print(f"[red]implementation files changed without a matching Qwen dispatch: "
                  f"{', '.join(rep['unattributed_changes'])}[/]")
    else:
        console.print("[green]all implementation changes since the session baseline are attributed to Qwen dispatches[/]")


@hero_app.command("unconfigure")
def hero_unconfigure(project: Path = typer.Argument(Path("."))) -> None:
    """Stop enforcing (keeps the evidence logs). Remove hooks from settings.local.json."""
    project = project.resolve()
    s = project / ".claude" / "settings.local.json"
    if s.exists():
        data = json.loads(s.read_text())
        for event, arr in list((data.get("hooks") or {}).items()):
            data["hooks"][event] = [h for h in arr if h.get("added_by") != conf.MARK]
            if not data["hooks"][event]:
                del data["hooks"][event]
        s.write_text(json.dumps(data, indent=2) + "\n")
    b = project / ".qwen-routing" / "config.json"
    if b.exists():
        d = json.loads(b.read_text())
        d["enforce"] = False
        b.write_text(json.dumps(d, indent=2) + "\n")
    console.print("enforcement disabled; hero.local.json model roles left as-is (edit or delete them if desired)")


def dispatch(
    project: Path = typer.Option(Path("."), help="Project root (the Hero repo)"),
    role: str = typer.Option(None, help="Qwen-routed role, e.g. engineer"),
    task: str = typer.Option(None),
    task_file: Path = typer.Option(None),
    context_file: Path = typer.Option(None),
    criteria: list[str] = typer.Option(None, "--criteria"),
    validate: list[str] = typer.Option(None, "--validate"),
    spec: str = typer.Option(None, help="Hero spec slug for provenance"),
    profile: str = typer.Option(None, help="Override the compute profile from Hero models.roles"),
    request: Path = typer.Option(None, help="Full DispatchRequest JSON instead of flags"),
    allow_network: bool = typer.Option(False),
    out: Path = typer.Option(None, help="Also write the result JSON here"),
) -> None:
    """Run a Qwen-assigned role on the project; prints a JSON DispatchResult."""
    c = cfg()
    if not request and not role:
        err.print("--role (or --request) is required")
        raise typer.Exit(2)
    req = build_request({"project": str(project), "role": role, "task": task,
                         "task_file": str(task_file) if task_file else None,
                         "context_file": str(context_file) if context_file else None, "criteria": criteria,
                         "validate": validate, "spec": spec, "profile": profile,
                         "request": str(request) if request else None, "allow_network": allow_network})
    result, code = run_dispatch(c, req, Path(req.repo_path))
    text = result.model_dump_json(indent=2)
    if out:
        out.write_text(text)
    print(text)
    raise typer.Exit(code)


@override_app.command("grant")
def override_grant(
    role: str = typer.Option(..., help="Agent name, or '*' for all Qwen-routed roles"),
    reason: str = typer.Option(..., help="Why Claude may take over (recorded)"),
    project: Path = typer.Option(Path(".")),
    ttl: str = typer.Option("1h"),
    scope: str = typer.Option("agent", help="agent: spawn the role on Claude | edits: Claude may edit implementation files"),
) -> None:
    """HUMAN ONLY: allow Claude to perform a Qwen-assigned role for a limited time."""
    if not is_tty():
        err.print("refused: overrides require an interactive terminal (Claude cannot grant them)")
        raise typer.Exit(3)
    confirm = typer.prompt(f"type the role name ({role}) to confirm Claude may do this work instead of Qwen")
    if confirm.strip() != role:
        err.print("confirmation mismatch; no override granted")
        raise typer.Exit(1)
    o = ov.grant(project, role, scope, reason, parse_duration(ttl) or 3600, getpass.getuser())
    console.print(f"override granted: {o.role} scope={o.scope} until {o.expires_at:.0f} (logged)")


@override_app.command("list")
def override_list(project: Path = typer.Option(Path("."))) -> None:
    for o in ov.load(project):
        console.print(f"{o.role} scope={o.scope} active={o.active()} reason={o.reason!r} by {o.granted_by}")


@override_app.command("revoke")
def override_revoke(project: Path = typer.Option(Path(".")), role: str = typer.Option(None)) -> None:
    console.print(f"revoked {ov.revoke(project, role)} override(s)")
