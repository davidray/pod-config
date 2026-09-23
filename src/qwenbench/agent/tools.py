"""Tools exposed to the Qwen coding agent (OpenAI function-calling format).

File tools are implemented in Python and confined to the workspace: every
path is resolved (following symlinks) and must stay under the workspace root.
Shell commands go through the OS sandbox in `sandbox.py`.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qwenbench.agent.sandbox import Sandbox

MAX_READ_LINES = 600
MAX_READ_CHARS = 48_000
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "dist", "build", ".tox", ".next", "target"}


class ToolError(Exception):
    pass


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


TOOL_SCHEMAS = [
    _fn("list_files", "List files in the repository (respects .gitignore). Use to orient yourself.",
        {"path": {"type": "string", "description": "Directory relative to repo root", "default": "."},
         "pattern": {"type": "string", "description": "Optional glob, e.g. '*.py' or 'src/**/*.ts'"}}, []),
    _fn("read_file", "Read a text file with line numbers. Read before editing.",
        {"path": {"type": "string"},
         "start_line": {"type": "integer", "description": "1-based, default 1"},
         "end_line": {"type": "integer", "description": "inclusive; default start+600"}}, ["path"]),
    _fn("search", "Search file contents with a regular expression (like grep -n).",
        {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."},
         "glob": {"type": "string", "description": "Optional filename glob filter"},
         "ignore_case": {"type": "boolean", "default": False}}, ["pattern"]),
    _fn("write_file", "Create or overwrite a file with the given full content.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("edit_file", "Replace an exact string in a file. old_string must match exactly once "
        "(include surrounding lines for uniqueness) unless replace_all is true.",
        {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
         "replace_all": {"type": "boolean", "default": False}}, ["path", "old_string", "new_string"]),
    _fn("run_command", "Run a shell command in the repository root inside a sandbox (no network, writes "
        "limited to the repo). Use for tests, linters, formatters, builds. Git commit/push are disabled.",
        {"command": {"type": "string"}, "timeout_s": {"type": "integer", "default": 180}}, ["command"]),
    _fn("finish", "Call exactly once when done. status=completed only if the work is done and validation "
        "passes; blocked if you need information you cannot get; failed if you cannot complete it.",
        {"status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
         "summary": {"type": "string", "description": "What you changed and why, and validation results"},
         "notes": {"type": "string", "description": "Caveats, follow-ups, or what blocked you"}},
        ["status", "summary"]),
]


@dataclass
class ToolOutcome:
    name: str
    ok: bool
    content: str
    finished: dict[str, Any] | None = None
    touched: str | None = None  # relative path written, if any


class Toolbox:
    def __init__(self, workspace: Path, sandbox: Sandbox, max_command_timeout_s: int = 900):
        self.root = Path(os.path.realpath(workspace))
        self.sandbox = sandbox
        self.max_command_timeout_s = max_command_timeout_s
        self._dispatch: dict[str, Callable[..., ToolOutcome]] = {
            "list_files": self.list_files, "read_file": self.read_file, "search": self.search,
            "write_file": self.write_file, "edit_file": self.edit_file, "run_command": self.run_command,
            "finish": self.finish,
        }

    # ---------------------------------------------------------------- dispatch

    def call(self, name: str, raw_args: str | dict[str, Any]) -> ToolOutcome:
        if name not in self._dispatch:
            return ToolOutcome(name, False, f"ERROR: unknown tool {name!r}. Available: {', '.join(self._dispatch)}")
        try:
            args = raw_args if isinstance(raw_args, dict) else (json.loads(raw_args) if raw_args.strip() else {})
        except json.JSONDecodeError as e:
            return ToolOutcome(name, False, f"ERROR: arguments are not valid JSON ({e}). Retry with valid JSON.")
        if not isinstance(args, dict):
            return ToolOutcome(name, False, "ERROR: arguments must be a JSON object")
        try:
            return self._dispatch[name](**args)
        except TypeError as e:
            return ToolOutcome(name, False, f"ERROR: bad arguments for {name}: {e}")
        except ToolError as e:
            return ToolOutcome(name, False, f"ERROR: {e}")

    # ---------------------------------------------------------------- paths

    def resolve(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path is required")
        candidate = (self.root / rel.strip()).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError(f"path {rel!r} is outside the repository")
        rel_parts = candidate.relative_to(self.root).parts
        if rel_parts and rel_parts[0] == ".git":
            raise ToolError("the .git directory is off limits")
        return candidate

    def resolve_writable(self, rel: str) -> Path:
        p = self.resolve(rel)
        for prot in self.sandbox.protected:
            prot = Path(os.path.realpath(prot))
            if p == prot or prot in p.parents:
                raise ToolError(f"{self.rel(p)} is protected configuration and cannot be modified")
        return p

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)) if p != self.root else "."

    # ---------------------------------------------------------------- tools

    def _all_files(self, base: Path) -> list[str]:
        try:
            out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "--", self.rel(base)],
                                 cwd=self.root, capture_output=True, text=True, timeout=30)
            if out.returncode == 0:
                return [line for line in out.stdout.splitlines() if line]
        except (OSError, subprocess.TimeoutExpired):
            pass
        files = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                files.append(self.rel(Path(dirpath) / f))
        return sorted(files)

    def list_files(self, path: str = ".", pattern: str | None = None) -> ToolOutcome:
        base = self.resolve(path)
        files = self._all_files(base)
        if pattern:
            files = [f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(os.path.basename(f), pattern)]
        shown = files[:400]
        more = f"\n... and {len(files) - 400} more (narrow with path/pattern)" if len(files) > 400 else ""
        return ToolOutcome("list_files", True, "\n".join(shown) + more if shown else "(no files)")

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> ToolOutcome:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"{path} is not a file")
        try:
            lines = p.read_text().splitlines()
        except UnicodeDecodeError as e:
            raise ToolError(f"{path} is not UTF-8 text") from e
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else start + MAX_READ_LINES - 1)
        body = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1))
        if len(body) > MAX_READ_CHARS:
            body = body[:MAX_READ_CHARS] + "\n...[truncated; request a smaller line range]"
        footer = f"\n[lines {start}-{end} of {len(lines)}]" if (start > 1 or end < len(lines)) else ""
        return ToolOutcome("read_file", True, (body or "(empty file)") + footer)

    def search(self, pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False) -> ToolOutcome:
        base = self.resolve(path)
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}") from e
        hits: list[str] = []
        for f in self._all_files(base):
            if glob and not (fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(os.path.basename(f), glob)):
                continue
            fp = self.root / f
            try:
                if fp.stat().st_size > 2_000_000:
                    continue
                for n, line in enumerate(fp.read_text(errors="strict").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f}:{n}: {line[:300]}")
                        if len(hits) >= 200:
                            break
            except (UnicodeDecodeError, OSError):
                continue
            if len(hits) >= 200:
                hits.append("... (200 match limit; refine the pattern)")
                break
        return ToolOutcome("search", True, "\n".join(hits) if hits else "(no matches)")

    def write_file(self, path: str, content: str) -> ToolOutcome:
        p = self.resolve_writable(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content)
        return ToolOutcome("write_file", True, f"{'overwrote' if existed else 'created'} {self.rel(p)} "
                           f"({len(content.splitlines())} lines)", touched=self.rel(p))

    def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> ToolOutcome:
        p = self.resolve_writable(path)
        if not p.is_file():
            raise ToolError(f"{path} does not exist; use write_file to create it")
        text = p.read_text()
        count = text.count(old_string)
        if not old_string:
            raise ToolError("old_string is empty")
        if count == 0:
            raise ToolError("old_string not found. Re-read the file and copy the exact text (whitespace matters).")
        if count > 1 and not replace_all:
            raise ToolError(f"old_string matches {count} times; add surrounding context or set replace_all")
        p.write_text(text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1))
        return ToolOutcome("edit_file", True, f"edited {self.rel(p)} ({count if replace_all else 1} replacement)",
                           touched=self.rel(p))

    def run_command(self, command: str, timeout_s: int = 180) -> ToolOutcome:
        timeout = max(5, min(int(timeout_s), self.max_command_timeout_s))
        r = self.sandbox.run(command, timeout_s=timeout)
        if r.refused:
            return ToolOutcome("run_command", False, r.output)
        status = "TIMED OUT" if r.timed_out else f"exit code {r.exit_code}"
        return ToolOutcome("run_command", r.ok, f"[{status}, {r.duration_s:.1f}s]\n{r.output}")

    def finish(self, status: str, summary: str, notes: str | None = None) -> ToolOutcome:
        if status not in ("completed", "blocked", "failed"):
            raise ToolError("status must be completed, blocked or failed")
        return ToolOutcome("finish", True, "finish recorded",
                           finished={"status": status, "summary": summary, "notes": notes})
