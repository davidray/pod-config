"""Claude Code hook handlers that enforce the Claude/Qwen boundary.

Installed by `qwen hero configure` into the project's
`.claude/settings.local.json` (Hero never touches that file). Hook input and
output follow https://code.claude.com/docs/en/hooks (verified 2026-09-23):
stdin JSON with tool_name / tool_input / agent_type; deny with exit code 2
plus a JSON `hookSpecificOutput.permissionDecision = "deny"`.

PreToolUse rules (only when <project>/.qwen-routing/config.json enforces):

  Agent/Task  subagent routed to Qwen      -> DENY, tell Claude to run `qwen dispatch`
              subagent denied/unknown      -> DENY
              subagent routed to frontier  -> allow
  Edit/Write/MultiEdit/NotebookEdit
              protected path               -> DENY (always, even with overrides)
              caller is frontier (main thread or design/review subagent)
                and path is not frontier-writable (specs/docs) -> DENY
  Bash        `qwen override ...`           -> DENY (humans only)
              in-place edits / redirects into implementation files, git apply/patch -> DENY
  Anything the override store explicitly allows is let through and logged.

Every decision is appended to .qwen-routing/decisions.jsonl.
"""

from __future__ import annotations

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
from qwenbench.hero.ledger import enforcement_active, log_decision
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
        f"  qwen dispatch --project {shlex.quote(str(project))} --role {route.agent} "
        f"--task-file <task.md> [--criteria '...'] [--validate '<test command>'] [--spec <slug>]\n"
        "Write the full task (spec excerpt, conventions, file pointers, acceptance criteria) to the task file "
        "first. The command prints a JSON DispatchResult. If it fails, STOP and report the failure to the "
        "human; do not implement the change yourself. Only a human can authorize Claude to take over "
        "(`qwen override grant`, run by the human in their own terminal)."
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
OVERRIDE_RE = re.compile(r"\bqwen\s+override\b")


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
                "Delegate with `qwen dispatch --role engineer ...` (or the specialized implementation role). "
                "If dispatch is failing, stop and surface the failure to the human instead of editing."
            ), "edit-impl-denied", caller)
        return Decision(True, "no file target", "edit-no-target", caller)

    if tool == "Bash":
        cmd = tin.get("command") or ""
        if OVERRIDE_RE.search(cmd):
            return Decision(False, "Overrides are human-only. Ask the human to run `qwen override grant` in their "
                            "own terminal if they want Claude to take over a Qwen-assigned role.",
                            "override-human-only", caller)
        if caller and caller.is_qwen:
            return Decision(True, "implementation role (override)", "bash-impl-role", caller)
        targets, blanket = _bash_write_targets(cmd)
        role = caller.agent if caller else "main-thread"
        edits_ov = overrides.find_active(project, role, "edits") or overrides.find_active(project, "*", "edits")
        if blanket and not edits_ov:
            return Decision(False, f"{blanket}. Implementation changes go through `qwen dispatch`.",
                            "bash-patch-denied", caller)
        for t in targets:
            rel = _rel(project, t, event.get("cwd"))
            if rel is None or rel.startswith(".."):
                continue
            if _matches(rel, enf.protected):
                return Decision(False, f"{rel} is protected routing configuration.", "protected-path", caller)
            if not _matches(rel, enf.frontier_writable) and not edits_ov:
                return Decision(False, f"shell write to implementation file {rel}; delegate via `qwen dispatch`.",
                                "bash-write-denied", caller)
        return Decision(True, "shell command allowed", "bash-ok", caller)

    return Decision(True, "tool not governed by routing policy", "ungoverned", caller)


def session_context(policy: RolePolicyFile, project: Path) -> str:
    from qwenbench.hero.policy import route_table

    models = effective_models(project)
    table = route_table(policy, models)
    qwen = sorted(r.agent for r in table if r.is_qwen)
    frontier = sorted(r.agent for r in table if r.is_frontier)
    execution = models.model_for("execution")
    return (
        "## Model routing policy (enforced by qwenbench hooks)\n"
        f"Implementation roles run on Qwen ({execution}) via `qwen dispatch`, never as Claude subagents and "
        "never by Claude editing implementation files directly:\n"
        f"  {', '.join(qwen)}\n"
        f"Claude performs: {', '.join(frontier)}.\n"
        "Claude may write specs and docs (.hero/**, docs/**, *.md). For implementation, write a precise task "
        "file (spec excerpt, conventions, files, acceptance criteria, validation command) and run "
        "`qwen dispatch --project <root> --role <role> --task-file <file> --validate '<cmd>'`. Review the JSON "
        "result and the diff. If dispatch fails beyond the retry policy, stop and report it; do not fall back "
        "to implementing it yourself. Only the human can grant an override."
    )


def _project_root(event: dict[str, Any]) -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd())


def main(kind: str, stdin: str | None = None) -> int:
    """Entry point for `qwen hook <kind>`; returns the process exit code."""
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
        from qwenbench.agent import snapshot

        try:
            tree = snapshot.snapshot(project)
            (project / ".qwen-routing").mkdir(exist_ok=True)
            (project / ".qwen-routing" / "baseline.json").write_text(json.dumps({
                "tree": tree, "session_id": event.get("session_id")}))
        except Exception:
            pass
        log_decision(project, event="session-start", session_id=event.get("session_id"))
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": session_context(policy, project)}}))
        return 0

    if kind == "stop":
        from qwenbench.hero.audit import unattributed_changes

        try:
            flagged = unattributed_changes(project, policy)
        except Exception:
            flagged = []
        if flagged:
            log_decision(project, event="audit-flag", files=flagged, session_id=event.get("session_id"))
            print(json.dumps({"systemMessage": (
                "qwenbench audit: implementation files changed this session without a matching Qwen dispatch: "
                + ", ".join(flagged[:10]) + (" ..." if len(flagged) > 10 else "")
                + ". Run `qwen hero audit` for details.")}))
        return 0
    return 0
