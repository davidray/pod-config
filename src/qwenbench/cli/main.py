"""`qwenbench`: provision Runpod Qwen, benchmark it, and route Hero roles to it."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

import typer

from qwenbench import __version__
from qwenbench.cli import infra
from qwenbench.cli.bench import bench_app
from qwenbench.cli.common import cfg, console, err
from qwenbench.cli.hero import dispatch, hero_app, override_app
from qwenbench.paths import repo_root, state_dir
from qwenbench.secrets import get_secret

app = typer.Typer(help=__doc__, no_args_is_help=True, pretty_exceptions_show_locals=False)

app.command()(infra.up)
app.command()(infra.down)
app.command()(infra.status)
app.command("list")(infra.list_)
app.command()(infra.endpoint)
app.command()(infra.logs)
app.command()(infra.chat)
app.command()(infra.keepalive)
app.command(hidden=True)(infra.guard)
app.command(hidden=True)(infra.hook)
app.command()(dispatch)
app.add_typer(infra.infra_app, name="infra")
app.add_typer(infra.storage_app, name="storage")
app.add_typer(bench_app, name="bench")
app.add_typer(hero_app, name="hero")
app.add_typer(override_app, name="override")

REQUIRED_TOOLS = {"git": "git --version", "uv": "uv --version"}
OPTIONAL_TOOLS = {"hero": "hero --version", "claude": "claude --version"}


@app.command()
def version() -> None:
    console.print(__version__)


@app.command()
def setup() -> None:
    """First-run setup: .env from the example, state dir, tool check."""
    root = repo_root()
    env = root / ".env"
    if not env.exists():
        shutil.copy(root / ".env.example", env)
        env.chmod(0o600)
        console.print(f"created {env} (chmod 600): add RUNPOD_API_KEY (HF_TOKEN optional)")
    else:
        console.print(f"{env} exists")
    console.print(f"state dir: {state_dir()}")
    for tool in REQUIRED_TOOLS:
        console.print(f"{'ok ' if shutil.which(tool) else 'MISSING'} {tool}")
    console.print("next: [bold]qwenbench doctor[/]")


@app.command()
def doctor(
    profile: list[str] = typer.Option(None, "--profile", "-p", help="Limit GPU checks to these profiles"),
    project: Path = typer.Option(None, help="Also check Hero integration for this project"),
) -> None:
    """Credentials, tools, config, GPU availability, endpoint health and model identity, Hero."""
    results: list[tuple[str, str, str]] = []

    def add(status: str, name: str, detail: str = "") -> None:
        results.append((status, name, detail))

    # tools
    for tool, cmd in {**REQUIRED_TOOLS, **OPTIONAL_TOOLS}.items():
        path = shutil.which(tool)
        required = tool in REQUIRED_TOOLS
        detail = ""
        if path and cmd:
            detail = subprocess.run(cmd.split(), capture_output=True, text=True).stdout.strip().splitlines()[0:1]
            detail = detail[0] if detail else path
        add("ok" if path else ("FAIL" if required else "warn"), f"tool {tool}", detail or (path or "not found"))
    from qwenbench.agent.sandbox import detect_backend

    sb = detect_backend()
    hint = "install bubblewrap (bwrap)" if platform.system() == "Linux" else "sandbox-exec missing"
    add("ok" if sb != "none" else "FAIL", "agent sandbox", sb if sb != "none" else hint)

    # config
    try:
        c = cfg()
        add("ok", "config", f"profiles: {', '.join(c.profile_names())}")
    except typer.Exit:
        add("FAIL", "config", "see error above")
        _print(results)
        raise typer.Exit(1) from None
    from qwenbench.cli.infra import render_profile

    stale = []
    for n in c.profile_names():
        path = repo_root() / "infra" / "runpod" / "rendered" / f"{n}.json"
        import json as _json
        if not path.exists() or path.read_text() != _json.dumps(render_profile(c, n), indent=2) + "\n":
            stale.append(n)
    add("warn" if stale else "ok", "rendered infra in sync", f"stale: {stale}; run `qwenbench infra render`" if stale else "")

    # secrets
    key = get_secret("RUNPOD_API_KEY")
    add("ok" if key else "FAIL", "RUNPOD_API_KEY", "set" if key else "export it or add to .env")
    add("ok", "HF_TOKEN", "set" if get_secret("HF_TOKEN") else "not set (model is not gated; optional)")
    env_file = repo_root() / ".env"
    if env_file.exists():
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=repo_root(),
                                 capture_output=True).returncode == 0
        add("FAIL" if tracked else "ok", ".env not tracked by git", "TRACKED: remove it from git now" if tracked else "")

    # Runpod
    if key:
        from qwenbench.cli.common import provider

        prov = provider(c)
        try:
            pods = prov.our_pods()
            add("ok", "Runpod API auth", f"{len(pods)} qwenbench pod(s)")
            for p in pods:
                add("warn" if p.is_live else "ok", f"pod {p.name}", f"{p.id} {p.status} ${p.cost_per_hr:.2f}/hr")
        except Exception as e:
            add("FAIL", "Runpod API auth", str(e))
            pods = []
        names = profile or c.profile_names()
        try:
            dcs = {d["id"]: d for d in prov.client.datacenters()}
        except Exception as e:
            dcs = {}
            add("warn", "datacenter catalog", str(e))
        for n in names:
            p = c.profile(n)
            try:
                cat = prov.client.gpu_catalog(p.gpu_type_id, cloud=p.cloud)
                g = cat[0] if cat else {}
                avail = g.get("availability")
                price = (g.get("price") or {}).get(p.cloud.lower())
                add("ok" if avail not in (None, "NONE") else "warn", f"{n}: GPU {p.gpu_type_id}",
                    f"availability={avail} live price=${price}/hr (config list ${c.pricing.gpu_rate(p.gpu_type_id, p.cloud)})")
            except Exception as e:
                add("FAIL", f"{n}: GPU {p.gpu_type_id}", f"catalog lookup failed: {e}")
            for dc in p.data_center_ids:
                d = dcs.get(dc)
                if not d:
                    add("warn", f"{n}: data center {dc}", "not in catalog")
                    continue
                gpu_av = next((x.get("availability") for x in d.get("gpuAvailability") or []
                               if x.get("id") == p.gpu_type_id), None)
                nv = d.get("networkVolumeTypes") or []
                ok = bool(nv) or p.storage.mode != "network-volume"
                add("ok" if ok and gpu_av not in (None, "NONE") else "warn", f"{n}: data center {dc}",
                    f"gpu={gpu_av} network volumes={nv}")
            try:
                vol = prov.find_volume(p) if p.storage.mode == "network-volume" else None
                add("ok", f"{n}: cache volume", f"{vol.name} {vol.id} in {vol.data_center}" if vol
                    else "not created yet (first `qwenbench up` creates it)")
            except Exception as e:
                add("warn", f"{n}: cache volume", str(e))
            ep = prov.endpoint(p)
            if ep:
                from qwenbench.providers.openai_compat import OpenAICompatibleModel

                m = OpenAICompatibleModel(ep)
                h = m.health()
                add("ok" if h["health_ok"] else "FAIL", f"{n}: endpoint health", ep.base_url)
                root = next((x.get("root") for x in h["models"] if x["id"] == ep.model), None)
                ident_ok = h["model_listed"] and root == p.model.hf_repo
                add("ok" if ident_ok else "FAIL", f"{n}: model identity",
                    f"served={[x['id'] for x in h['models']]} root={root} expected={p.model.hf_repo}")
                probe = m.probe()
                add("ok" if probe["ok"] else "FAIL", f"{n}: inference probe", str(probe.get("error") or probe))
            else:
                add("ok", f"{n}: endpoint", "not up")

    # Hero
    if shutil.which("hero"):
        v = subprocess.run(["hero", "--version"], capture_output=True, text=True).stdout.strip()
        add("ok", "hero", v)
        if project:
            from qwenbench.hero.configure import verify

            for ch in verify(c, project):
                add(ch["status"], f"hero: {ch['check']}", ch["detail"])
    else:
        add("warn", "hero", "not installed (needed only for Hero routing)")
    _print(results)
    raise typer.Exit(1 if any(s == "FAIL" for s, _, _ in results) else 0)


def _print(results: list[tuple[str, str, str]]) -> None:
    for status, name, detail in results:
        color = {"ok": "green", "warn": "yellow"}.get(status, "red")
        console.print(f"[{color}]{status:>4}[/] {name}  [dim]{detail}[/]", highlight=False)
    fails = sum(1 for s, _, _ in results if s == "FAIL")
    if fails:
        err.print(f"{fails} check(s) failed")


if __name__ == "__main__":
    app()
