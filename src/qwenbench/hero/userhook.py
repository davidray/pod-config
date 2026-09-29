"""User-level Claude Code hooks, so enforcement follows the repository, not one checkout.

Project hooks load from a session's project root. The Claude desktop app starts
each session in a fresh worktree, which has none of a checkout's untracked
files, so per-checkout hooks silently never ran there (first live trial, ADR
0006). Instead:

  ~/.claude/settings.json        our hook entries, tagged "added_by": "qwenbench"
  <state dir>/claude-hook.sh     gate: exits at once unless the session's repo has
                                 a routing binding (<git common dir>/qwenbench/config.json),
                                 so other projects pay one `git` call, not a Python start

Both files are protected from Claude in bound repos (hooks.decide_pre_tool_use).
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from qwenbench.paths import state_dir

MARK = "qwenbench"
EVENTS = {"PreToolUse": "pre-tool-use", "SessionStart": "session-start", "Stop": "stop"}
MATCHERS = {"PreToolUse": "Agent|Task|Edit|Write|MultiEdit|NotebookEdit|Bash", "SessionStart": "", "Stop": ""}
TIMEOUTS = {"PreToolUse": 30, "SessionStart": 60, "Stop": 60}


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def wrapper_path() -> Path:
    return state_dir() / "claude-hook.sh"


def wrapper_script(python: str = sys.executable) -> str:
    return f"""#!/bin/sh
# Installed by `qwenbench hero install-hooks`. Claude Code runs this for governed
# tool calls in every project; it hands off to qwenbench only when the session's
# repository has a routing binding.
kind="$1"
input=$(cat)
bound() {{
  [ -n "$1" ] || return 1
  common=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 1
  [ -f "$common/qwenbench/config.json" ] && return 0
  top=$(git -C "$1" rev-parse --show-toplevel 2>/dev/null) && [ -f "$top/.qwen-routing/config.json" ]
}}
cwd=$(printf '%s' "$input" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' | head -n 1)
if bound "$cwd" || bound "${{CLAUDE_PROJECT_DIR:-}}"; then
  # Exit code 2 is how a hook blocks; pass qwenbench's status through.
  printf '%s' "$input" | {shlex.quote(python)} -m qwenbench.cli.main hook "$kind"
  exit $?
fi
exit 0
"""


def settings_with_user_hooks(existing: dict, command: str) -> dict:
    data = json.loads(json.dumps(existing))
    hooks = data.setdefault("hooks", {})
    for event, kind in EVENTS.items():
        arr = [h for h in hooks.get(event, []) if h.get("added_by") != MARK]
        arr.append({"matcher": MATCHERS[event], "added_by": MARK, "hooks": [
            {"type": "command", "command": f"{shlex.quote(command)} {kind}", "timeout": TIMEOUTS[event]}]})
        hooks[event] = arr
    return data


def settings_without_user_hooks(existing: dict) -> dict:
    data = json.loads(json.dumps(existing))
    hooks = data.get("hooks") or {}
    for event in list(hooks):
        hooks[event] = [h for h in hooks[event] if h.get("added_by") != MARK]
        if not hooks[event]:
            del hooks[event]
    if "hooks" in data and not data["hooks"]:
        del data["hooks"]
    return data


def read_settings() -> dict:
    p = settings_path()
    return json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}


def installed() -> bool:
    hooks = read_settings().get("hooks") or {}
    ours = {e for e, arr in hooks.items() for h in arr if h.get("added_by") == MARK}
    return ours >= set(EVENTS) and wrapper_path().exists()


def planned_settings() -> tuple[str, str]:
    """(current, proposed) text of ~/.claude/settings.json."""
    p = settings_path()
    old = p.read_text() if p.exists() else ""
    return old, json.dumps(settings_with_user_hooks(read_settings(), str(wrapper_path())), indent=2) + "\n"


def install() -> None:
    w = wrapper_path()
    w.write_text(wrapper_script())
    w.chmod(0o755)
    p = settings_path()
    old, new = planned_settings()
    p.parent.mkdir(parents=True, exist_ok=True)
    if old:
        p.with_name(p.name + ".qwenbench.bak").write_text(old)
    p.write_text(new)


def uninstall() -> None:
    p = settings_path()
    if p.exists():
        p.write_text(json.dumps(settings_without_user_hooks(read_settings()), indent=2) + "\n")
    wrapper_path().unlink(missing_ok=True)
