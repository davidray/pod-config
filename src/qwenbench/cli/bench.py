"""`qwenbench bench ...`"""

from __future__ import annotations

import calendar
import json
import time
from pathlib import Path

import typer
from rich.table import Table

from qwenbench.bench import report as rep
from qwenbench.bench.costs import compute_costs
from qwenbench.bench.runner import Intervention, run_benchmark, verify_case, write_json
from qwenbench.bench.suite import load_case, load_suite
from qwenbench.cli.common import cfg, console, err, handle_errors, is_tty, provider
from qwenbench.paths import results_dir
from qwenbench.runpod.provider import UpOptions
from qwenbench.state import load_session

bench_app = typer.Typer(help="Benchmark real coding tasks against an endpoint", no_args_is_help=True)


def _interactive_hook(case, trial, attempt, repo_path: Path):
    console.print(f"[yellow]{case.id} trial {trial} failed validation after {attempt['attempt']} attempt(s).[/]")
    console.print(f"workspace: {repo_path}")
    choice = typer.prompt("[h]int + retry, [m]anual fix then revalidate, [s]kip", default="s")
    if choice.startswith("h"):
        return Intervention("hint", typer.prompt("hint for the agent"))
    if choice.startswith("m"):
        reason = typer.prompt("what are you fixing (recorded)")
        typer.prompt(f"edit files under {repo_path}, then press enter", default="", show_default=False)
        return Intervention("manual-fix", reason)
    return Intervention("skip", "no intervention")


def finalize_costs(run_dir: Path, fetch_billing: bool = False) -> dict:
    c = cfg()
    meta = json.loads((run_dir / "run.json").read_text())
    session = meta.get("session") or {}
    p = c.profile(meta["profile"])
    requested = session.get("requested_at")
    end = meta.get("pod_terminated_at") or calendar.timegm(time.strptime(meta["ended_at"], "%Y-%m-%dT%H:%M:%SZ"))
    lifetime = (end - requested) if requested else meta.get("benchmark_wall_s")
    billing_total = None
    if fetch_billing and session.get("pod_id"):
        start = time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(requested - 3600))
        try:
            b = provider(c).client.pod_billing(session["pod_id"], start)
            totals = (b.get("metadata") or {}).get("totals") or {}
            if b.get("records"):
                billing_total = float(totals.get("totalAmount") or 0)
                meta["billing"] = b
        except Exception as e:
            err.print(f"billing API unavailable ({e}); keeping estimates")
    costs = compute_costs(c.pricing, gpu_type_id=p.gpu_type_id, cloud=p.cloud, gpu_count=p.gpu_count,
                          observed_rate=session.get("cost_per_hr"), observed_rate_source=session.get("cost_source"),
                          lifetime_s=lifetime, storage_gb=p.storage.size_gb, storage_mode=p.storage.mode,
                          billing_total=billing_total)
    meta["costs"] = costs.to_dict()
    write_json(run_dir / "run.json", meta)
    return meta["costs"]


def write_reports(run_dir: Path) -> None:
    run = rep.load_run(run_dir)
    (run_dir / "report.md").write_text(rep.markdown_report(run))
    (run_dir / "tasks.csv").write_text(rep.tasks_csv(run))


@bench_app.command("run")
@handle_errors
def bench_run(
    profile: str = typer.Option(..., "--profile", "-p"),
    suite: str = typer.Option("daveeval", "--suite", "-s"),
    repeat: int = typer.Option(1, min=1),
    case: list[str] = typer.Option(None, "--case", help="Run only these case ids"),
    max_attempts: int = typer.Option(None, help="Override the suite's max attempts per task"),
    interactive: bool = typer.Option(False, help="Offer hint/manual-fix interventions on failure (recorded)"),
    auto_up: bool = typer.Option(False, help="Run `qwenbench up` first if the profile is not up"),
    down_after: bool = typer.Option(False, help="Terminate the pod when the run finishes"),
    keep_workspaces: bool = typer.Option(False),
    allow_unsandboxed: bool = typer.Option(False, help="Run without an OS sandbox (recorded in results)"),
) -> None:
    """Run a suite against a profile's endpoint; results go to results/<run-id>/."""
    c = cfg()
    p = c.profile(profile)
    s, cases = load_suite(suite)
    if case:
        cases = [x for x in cases if x.id in set(case)]
        if not cases:
            err.print(f"no cases matched {case}")
            raise typer.Exit(2)
    prov = provider(c)
    ep = prov.endpoint(p)
    if not ep:
        if not auto_up:
            err.print(f"{profile} is not up. Run `qwenbench up {profile}` or pass --auto-up.")
            raise typer.Exit(1)
        ep = prov.up(p, UpOptions(), progress=console.print)
    session = load_session(profile)
    try:
        run_dir = run_benchmark(
            c, p, ep, s, cases, repeat=repeat, max_attempts=max_attempts,
            session_public=session.public_dict() if session else None,
            cost_per_hr=session.cost_per_hr if session else None, allow_unsandboxed=allow_unsandboxed,
            keep_workspaces=keep_workspaces, on_failure=_interactive_hook if (interactive and is_tty()) else None,
            progress=console.print,
        )
    finally:
        if down_after:
            prov.down(p, reason="bench run --down-after")
    if down_after:
        meta = json.loads((run_dir / "run.json").read_text())
        meta["pod_terminated_at"] = time.time()
        write_json(run_dir / "run.json", meta)
    finalize_costs(run_dir)
    write_reports(run_dir)
    console.print()
    console.print(rep.headline(rep.load_run(run_dir)))
    console.print(f"\nresults: {run_dir}\nreport:  {run_dir / 'report.md'}")


@bench_app.command("report")
def bench_report(run: str, fmt: str = typer.Option("text", "--format", help="text | md | csv | json")) -> None:
    """Summarize one run (individual trials + aggregates)."""
    data = rep.load_run(run)
    if fmt == "md":
        console.print(rep.markdown_report(data), markup=False, highlight=False)
    elif fmt == "csv":
        console.print(rep.tasks_csv(data), markup=False, highlight=False)
    elif fmt == "json":
        console.print_json(json.dumps(rep.summarize(data)))
    else:
        console.print(rep.headline(data))
        t = Table()
        for col in ("case", "trial", "ok", "1st", "wall", "req", "out tok", "ttft", "tok/s", "files", "+/-", "failure"):
            t.add_column(col)
        for r in rep.task_rows(data):
            t.add_row(r["case_id"], str(r["trial"]), "yes" if r["success"] else "[red]no[/]",
                      "yes" if r["first_pass"] else "no", rep._fmt(r["wall_s"], "dur"), str(r["requests"]),
                      rep._fmt(r["output_tokens"]), rep._fmt(r["median_ttft_s"], "s"), rep._fmt(r["median_output_tps"]),
                      str(r["files_changed"]), f"+{r['lines_added']}/-{r['lines_deleted']}", r["failure"] or "")
        console.print(t)


@bench_app.command("compare")
def bench_compare(runs: list[str] = typer.Argument(..., help="Two or more run ids"),
                  out: Path = typer.Option(None, help="Also write Markdown here")) -> None:
    """Side-by-side comparison with a comparability check."""
    data = [rep.load_run(r) for r in runs]
    md = rep.compare_markdown(data)
    console.print(md, markup=False, highlight=False)
    if out:
        out.write_text(md)


@bench_app.command("list")
def bench_list() -> None:
    """Runs in results/."""
    for d in sorted(results_dir().iterdir()):
        if (d / "run.json").exists():
            s = rep.summarize(rep.load_run(d))
            console.print(f"{d.name}  {s['succeeded']}/{s['tasks']} ok  {rep._fmt(s['total_cost_usd'], 'usd')}")


@bench_app.command("finalize")
@handle_errors
def bench_finalize(run: str, billing: bool = typer.Option(True, help="Try the Runpod billing API")) -> None:
    """Recompute costs (optionally from authoritative billing, which can lag) and rewrite reports."""
    run_dir = rep.resolve_run(run)
    costs = finalize_costs(run_dir, fetch_billing=billing)
    write_reports(run_dir)
    console.print_json(json.dumps(costs))


@bench_app.command("evaluate")
def bench_evaluate(run: str, case_id: str, trial: int = typer.Option(1)) -> None:
    """Record your subjective 1-5 scores for one trial (stored separately from metrics)."""
    run_dir = rep.resolve_run(run)
    tdir = run_dir / "tasks" / case_id / f"trial-{trial}"
    if not tdir.exists():
        err.print(f"no such trial: {tdir}")
        raise typer.Exit(2)
    console.print(f"diff: {tdir / 'diff.patch'}")
    prompts = {
        "correctness": "correctness (1=wrong, 5=fully correct)",
        "code_quality": "code quality (1=poor, 5=excellent)",
        "architecture_adherence": "adherence to existing architecture (1=ignores, 5=seamless)",
        "unnecessary_changes": "unnecessary changes (1=many, 5=none)",
        "babysitting": "amount of babysitting required (1=constant, 5=none)",
    }
    scores = {k: typer.prompt(label, type=typer.IntRange(1, 5)) for k, label in prompts.items()}
    notes = typer.prompt("notes", default="", show_default=False)
    rec = {"case_id": case_id, "trial": trial, "scores": scores, "notes": notes,
           "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (tdir / "subjective.json").write_text(json.dumps(rec, indent=2))
    write_reports(run_dir)
    console.print("saved")


@bench_app.command("verify-cases")
def bench_verify_cases(suite: str = typer.Option("daveeval"), case: list[str] = typer.Option(None, "--case"),
                       allow_unsandboxed: bool = typer.Option(False)) -> None:
    """GPU-free harness self-test: base must FAIL validation, reference.patch must PASS."""
    _, cases = load_suite(suite)
    if case:
        cases = [load_case(c) for c in case]
    bad = 0
    for c in cases:
        r = verify_case(c, allow_unsandboxed=allow_unsandboxed)
        ok = r.get("ok")
        bad += not ok
        console.print(f"{'[green]ok[/]' if ok else '[red]FAIL[/]'}  {c.id}: base_fails={r.get('base_fails')} "
                      f"reference_passes={r.get('reference_passes')} start={str(r.get('starting_commit'))[:10]} "
                      f"{r.get('error', '')}")
        for f in r.get("reference_failures") or []:
            console.print(f"    {f['command']}\n{f['output_tail'][-1500:]}", markup=False)
    raise typer.Exit(1 if bad else 0)
