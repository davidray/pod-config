"""Load benchmark results and render reports/comparisons (Markdown + CSV).

Quality is never collapsed into a single score: every table shows raw
measures, individual trials are listed alongside aggregates, and subjective
scores live in their own section.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qwenbench.config import Config, format_duration
from qwenbench.paths import results_dir

SUBJECTIVE_KEYS = ("correctness", "code_quality", "architecture_adherence", "unnecessary_changes", "babysitting")


@dataclass
class RunData:
    run_dir: Path
    meta: dict[str, Any]
    tasks: list[dict[str, Any]]
    requests: list[dict[str, Any]]
    subjective: list[dict[str, Any]]

    @property
    def run_id(self) -> str:
        return self.meta.get("run_id", self.run_dir.name)


def resolve_run(ref: str) -> Path:
    p = Path(ref)
    if p.is_dir():
        return p
    base = results_dir()
    if (base / ref).is_dir():
        return base / ref
    matches = sorted(d for d in base.iterdir() if d.is_dir() and ref in d.name)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"no run matching {ref!r} in {base}")
    raise ValueError(f"{ref!r} is ambiguous: {', '.join(m.name for m in matches)}")


def load_run(ref: str | Path) -> RunData:
    run_dir = resolve_run(str(ref))
    meta = json.loads((run_dir / "run.json").read_text())
    tasks, subjective = [], []
    for rf in sorted((run_dir / "tasks").glob("*/trial-*/result.json")):
        tasks.append(json.loads(rf.read_text()))
        sf = rf.parent / "subjective.json"
        if sf.exists():
            subjective.append(json.loads(sf.read_text()))
    req_path = run_dir / "requests.jsonl"
    requests = [json.loads(line) for line in req_path.read_text().splitlines() if line.strip()] if req_path.exists() else []
    return RunData(run_dir, meta, tasks, requests, subjective)


def _median(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def summarize(run: RunData, cfg: Config | None = None) -> dict[str, Any]:
    t = run.tasks
    ok_req = [r for r in run.requests if r.get("ok")]
    succeeded = [x for x in t if x.get("success")]
    task_time = sum(x.get("wall_s") or 0 for x in t)
    costs = run.meta.get("costs") or {}
    total_cost = costs.get("total_usd")
    session = run.meta.get("session") or {}
    ready = (session.get("startup") or {}).get("ready") or {}
    return {
        "run_id": run.run_id,
        "profile": run.meta.get("profile"),
        "gpu": (run.meta.get("compute") or {}).get("gpu_type_id"),
        "tasks": len(t),
        "succeeded": len(succeeded),
        "first_pass": sum(1 for x in t if x.get("first_pass")),
        "interventions": sum(len(x.get("interventions") or []) for x in t),
        "total_task_s": round(task_time, 1),
        "median_task_s": _median([x.get("wall_s") for x in t]),
        "benchmark_wall_s": run.meta.get("benchmark_wall_s"),
        "gpu_startup_s": ready.get("since_request_s"),
        "gpu_lifetime_s": costs.get("gpu_lifetime_s"),
        "gpu_attributable_s": round(sum(x.get("gpu_attributable_s") or 0 for x in t), 1),
        "requests": len(run.requests),
        "failed_requests": sum(1 for r in run.requests if not r.get("ok")),
        "retries": sum(1 for r in run.requests if (r.get("attempt") or 1) > 1),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in ok_req),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in ok_req),
        "cached_input_tokens": sum(r.get("cached_input_tokens") or 0 for r in ok_req),
        "median_ttft_s": _median([r.get("ttft_s") for r in ok_req]),
        "p90_ttft_s": _pct([r.get("ttft_s") for r in ok_req], 0.9),
        "median_output_tps": _median([r.get("output_tokens_per_s") for r in ok_req]),
        "gpu_cost_usd": costs.get("billing_api_usd") if costs.get("billing_api_usd") is not None else costs.get("gpu_cost_usd"),
        "gpu_cost_source": costs.get("gpu_cost_source", "n/a"),
        "storage_cost_usd": costs.get("storage_cost_usd"),
        "total_cost_usd": total_cost,
        "cost_per_success_usd": round(total_cost / len(succeeded), 4) if (total_cost and succeeded) else None,
        "lines_added": sum(x.get("lines_added") or 0 for x in t),
        "lines_deleted": sum(x.get("lines_deleted") or 0 for x in t),
    }


def _pct(values: list[float | None], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(q * len(vals)))]


def _fmt(v: Any, kind: str = "") -> str:
    if v is None:
        return "-"
    if kind == "dur":
        return format_duration(v)
    if kind == "usd":
        return f"${v:,.2f}" if v >= 0.1 else f"${v:,.4f}"
    if kind == "s":
        return f"{v:.2f}s"
    if kind == "tps":
        return f"{v:.1f} tok/s"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def headline(run: RunData) -> str:
    s = summarize(run)
    lines = [
        f"Profile: {s['profile']} ({s['gpu']})",
        f"Tasks: {s['tasks']}",
        f"Succeeded: {s['succeeded']}",
        f"First-pass success: {s['first_pass']}",
        f"Interventions: {s['interventions']}",
        "",
        f"GPU startup:       {_fmt(s['gpu_startup_s'], 's')}",
        f"Benchmark runtime: {_fmt(s['benchmark_wall_s'], 'dur')}",
        f"GPU lifetime:      {_fmt(s['gpu_lifetime_s'], 'dur')}",
        "",
        f"Input tokens:      {_fmt(s['input_tokens'])}  (cached: {_fmt(s['cached_input_tokens'])})",
        f"Output tokens:     {_fmt(s['output_tokens'])}",
        f"Requests:          {_fmt(s['requests'])}  (failed: {s['failed_requests']}, retries: {s['retries']})",
        "",
        f"Median TTFT:       {_fmt(s['median_ttft_s'], 's')}  (p90 {_fmt(s['p90_ttft_s'], 's')})",
        f"Median output:     {_fmt(s['median_output_tps'], 'tps')}",
        "",
        f"GPU cost:          {_fmt(s['gpu_cost_usd'], 'usd')}  [{s['gpu_cost_source']}]",
        f"Storage estimate:  {_fmt(s['storage_cost_usd'], 'usd')}  [estimate, prorated network volume]",
        f"Total estimate:    {_fmt(s['total_cost_usd'], 'usd')}",
        "",
        f"Cost/success:      {_fmt(s['cost_per_success_usd'], 'usd')}",
    ]
    return "\n".join(lines)


TASK_COLUMNS = ["case_id", "trial", "success", "first_pass", "attempts", "wall_s", "requests", "input_tokens",
                "cached_input_tokens", "output_tokens", "median_ttft_s", "median_output_tps", "iterations",
                "tool_calls", "files_changed", "lines_added", "lines_deleted", "interventions", "failure",
                "estimated_compute_cost_usd"]


def task_rows(run: RunData) -> list[dict[str, Any]]:
    rows = []
    for x in sorted(run.tasks, key=lambda r: (r["case_id"], r["trial"])):
        rows.append({
            "case_id": x["case_id"], "trial": x["trial"], "success": x.get("success"),
            "first_pass": x.get("first_pass"), "attempts": len(x.get("attempts") or []),
            "wall_s": x.get("wall_s"), "requests": x.get("requests"), "input_tokens": x.get("input_tokens"),
            "cached_input_tokens": x.get("cached_input_tokens"), "output_tokens": x.get("output_tokens"),
            "median_ttft_s": x.get("median_ttft_s"), "median_output_tps": x.get("median_output_tps"),
            "iterations": x.get("iterations"), "tool_calls": x.get("tool_calls"),
            "files_changed": len(x.get("files_changed") or []), "lines_added": x.get("lines_added"),
            "lines_deleted": x.get("lines_deleted"), "interventions": len(x.get("interventions") or []),
            "failure": (x.get("failure") or {}).get("kind") if not x.get("success") else "",
            "estimated_compute_cost_usd": x.get("estimated_compute_cost_usd"),
        })
    return rows


def tasks_csv(run: RunData) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TASK_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in task_rows(run):
        w.writerow(r)
    return buf.getvalue()


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def subjective_section(run: RunData) -> str:
    if not run.subjective:
        return "_No subjective evaluations recorded. Use `qwen bench evaluate`._"
    rows = [[s["case_id"], s["trial"], *[s["scores"].get(k, "-") for k in SUBJECTIVE_KEYS], s.get("notes", "")]
            for s in run.subjective]
    return _md_table(["case", "trial", *SUBJECTIVE_KEYS, "notes"], rows)


def markdown_report(run: RunData) -> str:
    m = run.meta
    rt = m.get("runtime") or {}
    overrides = rt.get("hardware_overrides") or {}
    parts = [
        f"# Benchmark report: {run.run_id}",
        "```text\n" + headline(run) + "\n```",
        "## Configuration",
        _md_table(["field", "value"], [
            ["suite", m.get("suite")], ["repeat", m.get("repeat")], ["max attempts", m.get("max_attempts")],
            ["model", f"{m['model']['hf_repo']}@{m['model']['revision'][:12]}"],
            ["quantization", m["model"]["quantization"]], ["max context", m["model"]["max_model_len"]],
            ["image", rt.get("image")], ["vLLM", rt.get("vllm_version")], ["CUDA", rt.get("cuda_version")],
            ["GPU", f"{m['compute']['gpu_type_id']} x{m['compute']['gpu_count']} ({m['compute']['cloud']})"],
            ["data center", m["compute"].get("data_center_id")], ["pod", m["compute"].get("pod_id")],
            ["generation", json.dumps(m.get("generation"))],
            ["bench repo", f"{(m.get('agent') or {}).get('git_sha')} dirty={(m.get('agent') or {}).get('dirty')}"],
            ["sandbox", (m.get("agent") or {}).get("sandbox")],
        ]),
        ("**Hardware-specific runtime overrides: " + json.dumps(overrides) + "**") if overrides
        else "Runtime identical to the model default (no hardware overrides).",
        "vLLM invocation:\n\n```\n" + " ".join(rt.get("vllm_argv") or []) + "\n```",
        "## Individual trials",
        _md_table(TASK_COLUMNS, [[_fmt(r[c]) if not isinstance(r[c], bool) else ("yes" if r[c] else "no")
                                  for c in TASK_COLUMNS] for r in task_rows(run)]),
        "## Interventions",
        "\n".join(f"- {x['case_id']} trial {x['trial']}: {i['kind']}: {i['reason']}"
                  for x in run.tasks for i in (x.get("interventions") or [])) or "_None._",
        "## Subjective evaluation (kept separate from objective metrics)",
        subjective_section(run),
    ]
    return "\n\n".join(parts) + "\n"


COMPARE_ROWS = [
    ("successful tasks", "succeeded", ""), ("first-pass successes", "first_pass", ""),
    ("interventions", "interventions", ""), ("total task time", "total_task_s", "dur"),
    ("median task time", "median_task_s", "dur"), ("GPU startup", "gpu_startup_s", "s"),
    ("median TTFT", "median_ttft_s", "s"), ("p90 TTFT", "p90_ttft_s", "s"),
    ("median output tok/s", "median_output_tps", "tps"), ("input tokens", "input_tokens", ""),
    ("cached input tokens", "cached_input_tokens", ""), ("output tokens", "output_tokens", ""),
    ("failed requests", "failed_requests", ""), ("total GPU time", "gpu_lifetime_s", "dur"),
    ("GPU cost", "gpu_cost_usd", "usd"), ("storage estimate", "storage_cost_usd", "usd"),
    ("total estimated cost", "total_cost_usd", "usd"), ("cost per successful task", "cost_per_success_usd", "usd"),
]

COMPARABILITY_FIELDS = [
    ("model revision", lambda m: m["model"]["revision"]),
    ("max context", lambda m: m["model"]["max_model_len"]),
    ("image", lambda m: m["runtime"]["image"]),
    ("vLLM argv", lambda m: " ".join(m["runtime"]["vllm_argv"]).replace(m["model"]["served_model_name"], "")),
    ("hardware overrides", lambda m: json.dumps(m["runtime"].get("hardware_overrides") or {}, sort_keys=True)),
    ("generation", lambda m: json.dumps(m["generation"], sort_keys=True)),
    ("agent loop git sha", lambda m: (m.get("agent") or {}).get("git_sha")),
    ("suite", lambda m: m["suite"]),
    ("cases/prompts", lambda m: json.dumps({c["id"]: c["prompt_sha256"] for c in m["cases"]}, sort_keys=True)),
    ("starting commits", lambda m: json.dumps({c["id"]: sorted(set((c.get("starting_commits") or {}).values()))
                                               for c in m["cases"]}, sort_keys=True)),
    ("validation", lambda m: json.dumps({c["id"]: c["validation"] for c in m["cases"]}, sort_keys=True)),
    ("max attempts", lambda m: m.get("max_attempts")),
]


def comparability(runs: list[RunData]) -> list[str]:
    issues = []
    for label, fn in COMPARABILITY_FIELDS:
        try:
            values = {fn(r.meta) for r in runs}
        except (KeyError, TypeError):
            values = {"<missing>"}
        if len(values) > 1:
            issues.append(label)
    for r in runs:
        if (r.meta.get("agent") or {}).get("dirty"):
            issues.append(f"{r.run_id} ran from a dirty bench repo checkout")
        if (r.meta.get("runtime") or {}).get("hardware_overrides"):
            issues.append(f"{r.run_id} used hardware-specific runtime overrides")
    return issues


def compare_markdown(runs: list[RunData]) -> str:
    sums = [summarize(r) for r in runs]
    headers = ["metric", *[f"{s['profile']} ({s['run_id'][:22]})" for s in sums]]
    rows = [[label, *[_fmt(s[key], kind) for s in sums]] for label, key, kind in COMPARE_ROWS]
    per_case: dict[str, list[str]] = {}
    for i, r in enumerate(runs):
        for x in r.tasks:
            cell = per_case.setdefault(x["case_id"], ["-"] * len(runs))
            mark = "PASS" if x.get("success") else "fail"
            cell[i] = (cell[i] + " " if cell[i] != "-" else "") + f"{mark}({format_duration(x.get('wall_s'))})"
    issues = comparability(runs)
    out = [
        "## Comparison",
        _md_table(headers, rows),
        "## Per case (each trial listed; failures are never averaged away)",
        _md_table(["case", *headers[1:]], [[c, *v] for c, v in sorted(per_case.items())]),
        "## Comparability",
        ("**NOT directly comparable. These differ:**\n" + "\n".join(f"- {i}" for i in issues)) if issues
        else "Same model revision, runtime, generation settings, agent loop, prompts, starting commits and validation.",
    ]
    return "\n\n".join(out) + "\n"
