"""`qwenbench hero inspect|configure|verify` for a Hero-managed project.

What configure touches (and why each is safe from `hero upgrade`):

  .hero/hero.local.json         models.roles only; Hero's documented, git-ignored
                                local overlay (other keys preserved)
  .claude/settings.local.json   our hooks, tagged "added_by": "qwenbench"; Hero
                                only manages .claude/settings.json
  CLAUDE.local.md               a fenced qwenbench block; Hero manages CLAUDE.md
  .qwen-routing/config.json     our binding
  .git/info/exclude             keeps the above out of git without editing .gitignore

Nothing under .claude/agents, .claude/commands, .claude/skills, CLAUDE.md or
.hero/hero.json is modified (those are Hero-owned per .hero/install-state.json).
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qwenbench.config import Config
from qwenbench.hero import heroconfig
from qwenbench.hero.hooks import decide_pre_tool_use
from qwenbench.hero.ledger import DIR as ROUTING_DIR
from qwenbench.hero.policy import resolve, route_table
from qwenbench.paths import repo_root

MARK = "qwenbench"
BLOCK_START = "<!-- qwenbench:routing-start -->"
BLOCK_END = "<!-- qwenbench:routing-end -->"
EXCLUDES = [".qwen-routing/", "CLAUDE.local.md", ".claude/settings.local.json"]
DEFAULT_FRONTIER_MODEL = "claude-opus-5-5"


def run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def hook_command() -> str:
    """Absolute command for hooks, independent of the project's PATH.

    Uses the interpreter running this code rather than a PATH lookup: other
    tools install a `qwenbench` executable (e.g. Qwen Code), and a hook must never
    resolve to one of them.
    """
    return f"{shlex.quote(sys.executable)} -m qwenbench.cli.main hook"


# ------------------------------------------------------------------ inspect


def agent_inventory(project: Path) -> dict[str, Any]:
    agents_dir = project / ".claude" / "agents"
    installed = sorted(p.stem for p in agents_dir.glob("*.md")) if agents_dir.exists() else []
    state_file = project / ".hero" / "install-state.json"
    hero_owned: set[str] = set()
    targets: list[str] = []
    hero_version = None
    if state_file.exists():
        st = json.loads(state_file.read_text())
        hero_version = st.get("hero_version")
        for tname, t in (st.get("targets") or {}).items():
            targets.append(tname)
            hero_owned |= set(t.get("files") or [])
    owned_agents = sorted(Path(f).stem for f in hero_owned if f.startswith(".claude/agents/"))
    return {"installed": installed, "hero_owned": owned_agents,
            "user_agents": sorted(set(installed) - set(owned_agents)),
            "install_targets": targets, "install_hero_version": hero_version,
            "hero_owned_files": sorted(hero_owned)}


def inspect(cfg: Config, project: Path) -> dict[str, Any]:
    project = project.resolve()
    rc, version = run(["hero", "--version"], project)
    models = heroconfig.effective_models(project)
    inv = agent_inventory(project)
    classified = set(cfg.policy.agents) | set(cfg.policy.builtin_agents)
    table = route_table(cfg.policy, models, extra_agents=inv["installed"])
    rc_models, models_out = run(["hero", "models"], project)
    return {
        "project": str(project),
        "hero_installed": rc == 0,
        "hero_version": version if rc == 0 else None,
        "hero_workspace": (project / ".hero").is_dir(),
        "hero_models_cli": models_out,
        "models_base": (heroconfig.read_base(project).get("models") or {}),
        "models_local": (heroconfig.read_local(project).get("models") or {}),
        "models_effective": {"roles": models.roles, "default_model": models.default_model},
        "agents": inv,
        "unclassified_agents": sorted(set(inv["installed"]) - classified),
        "policy_agents_not_installed": sorted(set(cfg.policy.agents) - set(inv["installed"])),
        "routes": [r.to_dict() for r in table],
        "routing_configured": (project / ROUTING_DIR / "config.json").exists(),
        "hooks_installed": _hooks_installed(project),
    }


def _hooks_installed(project: Path) -> bool:
    p = project / ".claude" / "settings.local.json"
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return False
    return any(h.get("added_by") == MARK for arr in (data.get("hooks") or {}).values() for h in arr)


# ------------------------------------------------------------------ configure


@dataclass
class FileChange:
    path: Path
    old: str
    new: str

    def diff(self, project: Path) -> str:
        rel = self.path.relative_to(project)
        return "".join(difflib.unified_diff(self.old.splitlines(keepends=True), self.new.splitlines(keepends=True),
                                            fromfile=f"a/{rel}", tofile=f"b/{rel}"))

    @property
    def changed(self) -> bool:
        return self.old != self.new


@dataclass
class Plan:
    project: Path
    changes: list[FileChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def diff(self) -> str:
        return "\n".join(c.diff(self.project) for c in self.changes if c.changed)


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def settings_with_hooks(existing: dict[str, Any], command: str) -> dict[str, Any]:
    data = json.loads(json.dumps(existing))
    hooks = data.setdefault("hooks", {})
    wanted = {
        "PreToolUse": {"matcher": "Agent|Task|Edit|Write|MultiEdit|NotebookEdit|Bash",
                       "hooks": [{"type": "command", "command": f"{command} pre-tool-use", "timeout": 30}]},
        "SessionStart": {"matcher": "", "hooks": [{"type": "command", "command": f"{command} session-start",
                                                   "timeout": 60}]},
        "Stop": {"matcher": "", "hooks": [{"type": "command", "command": f"{command} stop", "timeout": 60}]},
    }
    for event, entry in wanted.items():
        arr = [h for h in hooks.get(event, []) if h.get("added_by") != MARK]
        arr.append({**entry, "added_by": MARK})
        hooks[event] = arr
    return data


def with_block(text: str, block: str) -> str:
    if BLOCK_START in text and BLOCK_END in text:
        pre = text[:text.index(BLOCK_START)]
        post = text[text.index(BLOCK_END) + len(BLOCK_END):].lstrip("\n")
        return pre + block + post
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block


ANY_PROFILE = "any"


def qwen_model_id(profile: str) -> str:
    """Hero model id for the execution role: plain "qwen" means any ready profile."""
    return "qwen" if profile == ANY_PROFILE else f"qwen:{profile}"


def plan_configure(cfg: Config, project: Path, execution_profile: str, frontier_model: str,
                   enforce: bool = True) -> Plan:
    project = project.resolve()
    if execution_profile != ANY_PROFILE:
        cfg.profile(execution_profile)  # validate
    plan = Plan(project)
    if not (project / ".hero").is_dir():
        plan.notes.append("no .hero/ workspace: run `hero init` first (routing still works from hero.local.json)")

    local_path = project / ".hero" / "hero.local.json"
    local = heroconfig.read_local(project)
    roles = {"design": frontier_model, "execution": qwen_model_id(execution_profile), "review": frontier_model}
    new_local = heroconfig.with_model_roles(local, roles)
    plan.changes.append(FileChange(local_path, heroconfig.render(local) if local else _read(local_path),
                                   heroconfig.render(new_local)))

    settings_path = project / ".claude" / "settings.local.json"
    existing = json.loads(_read(settings_path) or "{}")
    plan.changes.append(FileChange(settings_path, _read(settings_path),
                                   json.dumps(settings_with_hooks(existing, hook_command()), indent=2) + "\n"))

    binding_path = project / ROUTING_DIR / "config.json"
    binding = json.loads(_read(binding_path) or "{}")
    binding.update({"enforce": enforce, "bench_home": str(repo_root()), "execution_profile": execution_profile,
                    "frontier_model": frontier_model})
    binding.setdefault("sandbox", {"network": False, "real_home": False, "extra_writable": []})
    plan.changes.append(FileChange(binding_path, _read(binding_path), json.dumps(binding, indent=2) + "\n"))

    # The block text depends on the new roles; render with them applied.
    tmp_models = heroconfig.HeroModels({**heroconfig.effective_models(project).roles, **roles}, None)
    block = claude_local_block_for(cfg, tmp_models)
    md_path = project / "CLAUDE.local.md"
    plan.changes.append(FileChange(md_path, _read(md_path), with_block(_read(md_path), block)))

    exclude = git_exclude_path(project)
    if exclude:
        old = _read(exclude)
        missing = [e for e in EXCLUDES if e not in old.split("\n")]
        new = old + ("" if not old or old.endswith("\n") else "\n") + "".join(f"{e}\n" for e in missing)
        plan.changes.append(FileChange(exclude, old, new))
    return plan


def git_exclude_path(project: Path) -> Path | None:
    """info/exclude of the repo; in a linked worktree `.git` is a file, so ask git."""
    p = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=project,
                       capture_output=True, text=True)
    return Path(p.stdout.strip()) / "info" / "exclude" if p.returncode == 0 else None


def claude_local_block_for(cfg: Config, models: heroconfig.HeroModels) -> str:
    table = route_table(cfg.policy, models)
    qwen = sorted(r.agent for r in table if r.is_qwen)
    frontier = sorted(r.agent for r in table if r.is_frontier)
    body = (
        "## Model routing policy (enforced by qwenbench hooks)\n\n"
        f"Implementation roles run on **Qwen ({models.model_for('execution')})** through `qwenbench dispatch`; "
        "they are never spawned as Claude subagents, and Claude never edits implementation files itself:\n\n"
        f"{', '.join(qwen)}\n\n"
        f"Claude performs design, planning and review roles: {', '.join(frontier)}.\n\n"
        "To delegate: write a task file (spec excerpt, conventions, files to touch, acceptance criteria), then\n"
        "`qwenbench dispatch --project \"$CLAUDE_PROJECT_DIR\" --role <role> --task-file <file> --validate '<test cmd>' "
        "[--spec <slug>]`.\n"
        "Read the JSON DispatchResult and review the diff. If dispatch fails beyond the retry policy, stop and "
        "report the failure. Do not implement the work yourself and do not switch it to a Claude subagent: only "
        "the human can authorize that with `qwenbench override grant` in their own terminal.\n"
    )
    return (f"{BLOCK_START}\n<!-- Managed by `qwenbench hero configure` (qwenbench). "
            f"Edit config/role-policy.yaml in the qwenbench repo instead. -->\n{body}{BLOCK_END}\n")


def apply(plan: Plan) -> list[Path]:
    written = []
    for c in plan.changes:
        if not c.changed:
            continue
        c.path.parent.mkdir(parents=True, exist_ok=True)
        if c.path.exists():
            shutil.copy2(c.path, c.path.with_name(c.path.name + ".qwenbench.bak"))
        c.path.write_text(c.new)
        written.append(c.path)
    return written


# ------------------------------------------------------------------ verify


def simulate(cfg: Config, project: Path) -> list[dict[str, Any]]:
    """Feed synthetic hook events through the real decision function."""
    cases = [
        ("spawn engineer (Qwen role)", {"tool_name": "Agent", "tool_input": {"subagent_type": "engineer"}}, False),
        ("spawn api-engineer (Qwen role)", {"tool_name": "Agent", "tool_input": {"subagent_type": "api-engineer"}}, False),
        ("spawn brownfield-architect (Claude role)",
         {"tool_name": "Agent", "tool_input": {"subagent_type": "brownfield-architect"}}, True),
        ("spawn general-purpose (loophole)", {"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose"}}, False),
        ("main thread edits src file", {"tool_name": "Edit", "tool_input": {"file_path": str(project / "src/app.py")}}, False),
        ("main thread writes a spec", {"tool_name": "Write",
                                       "tool_input": {"file_path": str(project / ".hero/specs/x/spec.md")}}, True),
        ("main thread edits routing config", {"tool_name": "Edit",
                                              "tool_input": {"file_path": str(project / ".claude/settings.local.json")}}, False),
        ("claude grants itself an override", {"tool_name": "Bash", "tool_input": {"command": "qwenbench override grant --role engineer"}}, False),
        ("shell redirect into src", {"tool_name": "Bash", "tool_input": {"command": "echo x > src/app.py"}}, False),
        ("run tests", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}, True),
        ("dispatch to qwen", {"tool_name": "Bash", "tool_input": {"command": "qwenbench dispatch --role engineer --task-file t.md"}}, True),
    ]
    out = []
    for label, event, expect_allow in cases:
        event = {**event, "cwd": str(project)}
        d = decide_pre_tool_use(event, cfg.policy, project)
        out.append({"case": label, "expected": "allow" if expect_allow else "deny",
                    "actual": "allow" if d.allow else "deny", "ok": d.allow == expect_allow, "rule": d.rule})
    return out


def verify(cfg: Config, project: Path, endpoint_check=None) -> list[dict[str, Any]]:
    project = project.resolve()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        checks.append({"check": name, "status": "ok" if ok else ("warn" if warn else "FAIL"), "detail": detail})

    info = inspect(cfg, project)
    add("hero installed", info["hero_installed"], info["hero_version"] or "hero not on PATH")
    add("hero workspace", info["hero_workspace"], str(project / ".hero"))
    eff = info["models_effective"]["roles"]
    execution = resolve(cfg.policy, "engineer", heroconfig.effective_models(project))
    add("execution role -> qwen", execution.is_qwen, f"execution={eff.get('execution')}")
    add("design/review -> frontier", all(not str(eff.get(r, "")).startswith("qwen") and eff.get(r)
                                         for r in ("design", "review")),
        f"design={eff.get('design')} review={eff.get('review')}")
    rc, out = run(["hero", "models", "--check"], project)
    add("hero models --check", rc == 0, out.splitlines()[-1] if out else "")
    add("all installed agents classified", not info["unclassified_agents"],
        ", ".join(info["unclassified_agents"]) or "all classified")
    add("routing binding", info["routing_configured"], str(project / ROUTING_DIR / "config.json"))
    add("hooks in .claude/settings.local.json", info["hooks_installed"])
    cmd = shlex.split(hook_command())[0]
    add("hook command executable", Path(cmd).exists(), cmd)
    for s in simulate(cfg, project):
        add(f"policy: {s['case']}", s["ok"], f"expected {s['expected']}, got {s['actual']} ({s['rule']})")
    # A real hook round-trip through the installed command (exercises the actual process boundary).
    if info["hooks_installed"]:
        event = json.dumps({"tool_name": "Agent", "tool_input": {"subagent_type": "engineer"}, "cwd": str(project),
                            "session_id": "qwen-hero-verify"})
        try:
            p = subprocess.run([*shlex.split(hook_command()), "pre-tool-use"], input=event, capture_output=True, text=True,
                               cwd=project, env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)}, timeout=60)
            add("installed hook denies engineer spawn", p.returncode == 2 and "qwenbench dispatch" in p.stderr,
                f"exit={p.returncode}")
        except (OSError, subprocess.TimeoutExpired) as e:
            add("installed hook denies engineer spawn", False, str(e))
    if endpoint_check:
        ok, detail = endpoint_check(execution.profile)
        add("execution endpoint healthy", ok, detail, warn=not ok)
    rc, out = run(["hero", "check"], project, timeout=180)
    add("hero check", rc == 0, (out.splitlines()[-1] if out else ""), warn=True)
    return checks

