"""Claude Code hook handlers that enforce the Claude/Qwen boundary.

Installed by `qwenbench hero configure` into the project's
`.claude/settings.local.json` (Hero never touches that file). Hook input and
output follow https://code.claude.com/docs/en/hooks (verified 2026-09-23):
stdin JSON with tool_name / tool_input / agent_type; deny with exit code 2
plus a JSON `hookSpecificOutput.permissionDecision = "deny"`.

PreToolUse rules (only when <project>/.qwen-routing/config.json enforces):

  Agent/Task  subagent routed to Qwen      -> DENY, tell Claude to run `qwenbench dispatch`
              subagent denied/unknown      -> DENY
              subagent routed to frontier  -> allow
  Edit/Write/MultiEdit/NotebookEdit
              protected path               -> DENY (always, even with overrides)
              caller is frontier (main thread or design/review subagent)
                and path is not frontier-writable (specs/docs) -> DENY
  Bash        `qwenbench override ...`           -> DENY (humans only)
              in-place edits / redirects into implementation files, git apply/patch -> DENY
  Anything the override store explicitly allows is let through and logged.

Every decision is appended to .qwen-routing/decisions.jsonl.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qwenbench.config import RolePolicyFile
from qwenbench.hero import override as overrides
from qwenbench.hero.heroconfig import effective_models
from qwenbench.hero.ledger import (
    enforcement_active,
    git_toplevel,
    load_binding,
    log_decision,
    repo_binding_path,
)
from qwenbench.hero.policy import Route, resolve

AGENT_TOOLS = {"Agent", "Task"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


@dataclass
class Decision:
    allow: bool
    reason: str
    rule: str
    route: Route | None = None

    def hook_output(self) -> dict[str, Any]:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if self.allow else "deny",
            "permissionDecisionReason": self.reason,
        }} if not self.allow else {}


def dispatch_instructions(route: Route, project: Path) -> str:
    return (
        f"Role '{route.agent}' is assigned to Qwen ({route.model_id}) by the project model policy "
        f"({route.reason}). Claude must not perform it or spawn it as a Claude subagent. Delegate it:\n"
        f"  qwenbench dispatch --project {shlex.quote(str(project))} --role {route.agent} "
        f"--task-file <task.md> [--criteria '...'] [--validate '<test command>'] [--spec <slug>]\n"
        "Write the full task (spec excerpt, conventions, file pointers, acceptance criteria) to the task file "
        "first. The command prints a JSON DispatchResult. If it fails, STOP and report the failure to the "
        "human; do not implement the change yourself. Only a human can authorize Claude to take over "
        "(`qwenbench override grant`, run by the human in their own terminal)."
    )


def _rel(project: Path, path: str, cwd: str | None) -> str | None:
    p = Path(path)
    if not p.is_absolute():
        p = Path(cwd or project) / p
    try:
        return os.path.relpath(os.path.realpath(p), os.path.realpath(project))
    except ValueError:
        return None


def _matches(rel: str, globs: list[str]) -> bool:
    for g in globs:
        if fnmatch.fnmatch(rel, g):
            return True
        if g.endswith("/**") and (rel == g[:-3] or rel.startswith(g[:-2])):
            return True
        if g.startswith("**/") and fnmatch.fnmatch(os.path.basename(rel), g[3:]):
            return True
    return False


def _edit_targets(tool: str, tool_input: dict[str, Any]) -> list[str]:
    for key in ("file_path", "notebook_path", "path"):
        if tool_input.get(key):
            return [tool_input[key]]
    return []


REDIRECT_RE = re.compile(r"(?<![0-9&<>])>{1,2}\s*([^\s;&|<>]+)")
TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?([^\s;&|]+)")
INPLACE_RE = re.compile(r"\b(?:sed|gsed|perl|ruby)\b[^;&|]*\s-[a-zA-Z]*i[a-zA-Z]*\b[^;&|]*?\s([^\s;&|]+)\s*(?:$|[;&|])")
PATCH_RE = re.compile(r"\b(git\s+(apply|am|checkout\s+[^;&|]*--\s)|patch\s)")
# Commands that grant overrides or switch enforcement off: humans only.
OVERRIDE_RE = re.compile(r"\bqwen(bench)?\s+(override|hero\s+(unconfigure|uninstall-hooks|install-hooks|configure))\b")


def _bash_write_targets(command: str) -> tuple[list[str], str | None]:
    """Best-effort extraction of files a shell command writes. Returns (paths, blanket_reason)."""
    if PATCH_RE.search(command):
        return [], "applying patches/checkouts from the shell can rewrite implementation files"
    targets = [m.group(1) for m in REDIRECT_RE.finditer(command)]
    targets += [m.group(1) for m in TEE_RE.finditer(command)]
    targets += [m.group(1) for m in INPLACE_RE.finditer(command)]
    return [t.strip("'\"") for t in targets if t not in ("/dev/null", "/dev/stderr", "/dev/stdout")], None


def decide_pre_tool_use(event: dict[str, Any], policy: RolePolicyFile, project: Path) -> Decision:
    tool = event.get("tool_name") or ""
    tin = event.get("tool_input") or {}
    models = effective_models(project)
    caller_agent = event.get("agent_type")
    caller = resolve(policy, caller_agent, models) if caller_agent else None
    enf = policy.enforcement

    if tool in AGENT_TOOLS:
        sub = tin.get("subagent_type") or tin.get("type") or "general-purpose"
        route = resolve(policy, sub, models)
        if route.is_frontier:
            return Decision(True, route.reason, "agent-frontier", route)
        if route.is_qwen:
            ov = overrides.find_active(project, route.agent, "agent")
            if ov:
                return Decision(True, f"human override until {ov.expires_at:.0f}: {ov.reason}", "agent-override", route)
            return Decision(False, dispatch_instructions(route, project), "agent-qwen-must-dispatch", route)
        ov = overrides.find_active(project, route.agent, "agent")
        if ov:
            return Decision(True, f"human override: {ov.reason}", "agent-override", route)
        return Decision(False, f"Subagent '{route.agent}' is not permitted: {route.reason}. "
                        "Classify it in config/role-policy.yaml of the qwenbench repo if it should run.",
                        "agent-denied", route)

    if tool in EDIT_TOOLS:
        for target in _edit_targets(tool, tin):
            if _protected_outside(target, project, event.get("cwd")):
                return Decision(False, f"{target} is qwenbench routing configuration; only a human may change it.",
                                "protected-path", caller)
            rel = _rel(project, target, event.get("cwd"))
            if rel is None or rel.startswith(".."):
                continue  # outside the project: not our boundary
            if _matches(rel, enf.protected):
                return Decision(False, f"{rel} is protected routing configuration; only a human may change it.",
                                "protected-path", caller)
            if caller and caller.is_qwen:
                return Decision(True, f"{caller.agent} is an implementation role (running under override)",
                                "edit-by-impl-role", caller)
            if _matches(rel, enf.frontier_writable):
                return Decision(True, f"{rel} is a spec/doc path the frontier side may write", "edit-frontier-ok", caller)
            role = caller.agent if caller else "main-thread"
            ov = overrides.find_active(project, role, "edits") or overrides.find_active(project, "*", "edits")
            if ov:
                return Decision(True, f"human override (edits): {ov.reason}", "edit-override", caller)
            return Decision(False, (
                f"{rel} is an implementation file. Under this project's model policy Claude "
                f"({role}) plans, designs and reviews; implementation is performed by the Qwen worker. "
                "Delegate with `qwenbench dispatch --role engineer ...` (or the specialized implementation role). "
                "If dispatch is failing, stop and surface the failure to the human instead of editing."
            ), "edit-impl-denied", caller)
        return Decision(True, "no file target", "edit-no-target", caller)

    if tool == "Bash":
        cmd = tin.get("command") or ""
        if OVERRIDE_RE.search(cmd):
            return Decision(False, "Overrides and routing configuration are human-only. Ask the human to run "
                            "`qwenbench override grant` in their own terminal if they want Claude to take over a "
                            "Qwen-assigned role.",
                            "override-human-only", caller)
        if caller and caller.is_qwen:
            return Decision(True, "implementation role (override)", "bash-impl-role", caller)
        targets, blanket = _bash_write_targets(cmd)
        role = caller.agent if caller else "main-thread"
        edits_ov = overrides.find_active(project, role, "edits") or overrides.find_active(project, "*", "edits")
        if blanket and not edits_ov:
            return Decision(False, f"{blanket}. Implementation changes go through `qwenbench dispatch`.",
                            "bash-patch-denied", caller)
        for t in targets:
            if _protected_outside(t, project, event.get("cwd")):
                return Decision(False, f"{t} is qwenbench routing configuration.", "protected-path", caller)
            rel = _rel(project, t, event.get("cwd"))
            if rel is None or rel.startswith(".."):
                continue
            if _matches(rel, enf.protected):
                return Decision(False, f"{rel} is protected routing configuration.", "protected-path", caller)
            if not _matches(rel, enf.frontier_writable) and not edits_ov:
                return Decision(False, f"shell write to implementation file {rel}; delegate via `qwenbench dispatch`.",
                                "bash-write-denied", caller)
        return Decision(True, "shell command allowed", "bash-ok", caller)

    return Decision(True, "tool not governed by routing policy", "ungoverned", caller)


def routing_rules(qwen_model: str | None, qwen_roles: list[str], frontier_roles: list[str]) -> str:
    """The instructions Claude gets (CLAUDE.local.md and SessionStart). Shaped by the first live trial."""
    return (
        f"Implementation roles run on **Qwen ({qwen_model})** through `qwenbench dispatch`. Never spawn them as "
        "subagents (the hooks deny it; go straight to dispatch) and never edit implementation files yourself:\n"
        f"{', '.join(qwen_roles)}\n\n"
        f"Claude performs design, planning and review roles: {', '.join(frontier_roles)}. Claude may write specs and "
        "docs (.hero/**, docs/**, *.md).\n\n"
        "Delegating:\n"
        "1. Split the work into small dispatches, one coherent unit each (for example: core logic, then UI), "
        "about 3 files or one package at most. Dispatch the next unit after reviewing the previous one.\n"
        "2. The task file states behavior, interfaces (signatures, types), files to touch, constraints, acceptance "
        "criteria and the validation commands. Do not write the implementation code in it: that is the worker's job.\n"
        "3. Run `qwenbench dispatch --project \"$CLAUDE_PROJECT_DIR\" --role <role> --task-file <file> "
        "--validate '<cmd>' [--spec <slug>]` and read the JSON DispatchResult.\n"
        "4. Review the diff. Reject the result if `failure.kind` is `tests_disabled` or `warnings` is non-empty "
        "until you have checked each warning; tests that are commented out, skipped or weakened are not a pass.\n"
        "5. If a unit fails beyond the retry policy, stop and report it. Do not implement it yourself or hand it "
        "to a Claude subagent: only the human can authorize that with `qwenbench override grant` in their own "
        "terminal.\n"
    )


def session_context(policy: RolePolicyFile, project: Path) -> str:
    from qwenbench.hero.policy import route_table

    models = effective_models(project)
    table = route_table(policy, models)
    return "## Model routing policy (enforced by qwenbench hooks)\n\n" + routing_rules(
        models.model_for("execution"), sorted(r.agent for r in table if r.is_qwen),
        sorted(r.agent for r in table if r.is_frontier))


def _project_root(event: dict[str, Any]) -> Path:
    """The repository the session is working in right now.

    Prefer the event's cwd over CLAUDE_PROJECT_DIR: a session can start in one
    checkout (e.g. a fresh desktop-app worktree) and move into another. A file
    edit in a different bound repository is judged by that repository.
    """
    start = Path(event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    project = git_toplevel(start) or start
    for target in _edit_targets(event.get("tool_name") or "", event.get("tool_input") or {}):
        p = Path(target) if Path(target).is_absolute() else start / target
        other = git_toplevel(p)
        if other and other != project and load_binding(other):
            return other
    return project


def _protected_outside(path: str, project: Path, cwd: str | None) -> bool:
    """Routing files that live outside the checkout: the repo binding and the user-level hooks."""
    from qwenbench.hero import userhook

    p = Path(path) if Path(path).is_absolute() else Path(cwd or project) / path
    real = os.path.realpath(p)
    guarded = [userhook.settings_path(), userhook.wrapper_path()]
    binding = repo_binding_path(project)
    if binding:
        guarded.append(binding.parent)
    return any(real == os.path.realpath(g) or real.startswith(os.path.realpath(g) + os.sep) for g in guarded)


def main(kind: str, stdin: str | None = None) -> int:
    """Entry point for `qwenbench hook <kind>`; returns the process exit code."""
    from qwenbench.config import load_config

    raw = stdin if stdin is not None else sys.stdin.read()
    try:
        event = json.loads(raw or "{}")
    except ValueError:
        event = {}
    project = _project_root(event)
    if not enforcement_active(project):
        return 0
    try:
        policy = load_config().policy
    except Exception as e:  # fail closed: broken config must not silently allow
        if kind == "pre-tool-use" and event.get("tool_name") in AGENT_TOOLS | EDIT_TOOLS:
            msg = f"qwenbench routing config failed to load ({e}); refusing until fixed."
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": msg}}))
            print(msg, file=sys.stderr)
            return 2
        return 0

    if kind == "pre-tool-use":
        from qwenbench.hero.audit import record_baseline

        # A session that started in another checkout never fires SessionStart here.
        with contextlib.suppress(Exception):
            record_baseline(project, event.get("session_id"))
        d = decide_pre_tool_use(event, policy, project)
        tin = event.get("tool_input") or {}
        log_decision(project, event="pre-tool-use", tool=event.get("tool_name"), allow=d.allow, rule=d.rule,
                     caller=event.get("agent_type") or "main-thread",
                     subagent=tin.get("subagent_type") or tin.get("type"),
                     target=tin.get("file_path") or tin.get("notebook_path") or (tin.get("command") or "")[:200] or None,
                     route=d.route.to_dict() if d.route else None, reason=d.reason[:500],
                     session_id=event.get("session_id"))
        if d.allow:
            return 0
        print(json.dumps(d.hook_output()))
        print(d.reason, file=sys.stderr)
        return 2

    if kind == "session-start":
        from qwenbench.hero.audit import record_baseline

        with contextlib.suppress(Exception):
            record_baseline(project, event.get("session_id"))
        log_decision(project, event="session-start", session_id=event.get("session_id"))
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": session_context(policy, project)}}))
        return 0

    if kind == "stop":
        from qwenbench.hero.audit import unattributed_changes

        try:
            flagged = unattributed_changes(project, policy, event.get("session_id"))
        except Exception:
            flagged = []
        if flagged:
            log_decision(project, event="audit-flag", files=flagged, session_id=event.get("session_id"))
            print(json.dumps({"systemMessage": (
                "qwenbench audit: implementation files changed this session without a matching Qwen dispatch: "
                + ", ".join(flagged[:10]) + (" ..." if len(flagged) > 10 else "")
                + ". Run `qwenbench hero audit` for details.")}))
        return 0
    return 0
