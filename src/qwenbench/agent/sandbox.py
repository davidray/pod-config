"""Command sandbox for the Qwen coding agent.

Every shell command the agent runs goes through `Sandbox.run()`:

  * cwd is the task workspace; HOME/TMPDIR point into a private scratch dir
  * the environment is rebuilt from an allowlist (no RUNPOD_API_KEY, HF_TOKEN,
    ANTHROPIC_*, SSH_AUTH_SOCK, cloud credentials...)
  * a static policy rejects obviously dangerous commands before execution
  * on macOS, `sandbox-exec` confines writes to the workspace + scratch dir,
    blocks outbound network except loopback, and blocks reads of credential
    stores (profile verified on macOS 26, see docs/adr/0007)
  * on Linux, bubblewrap gives the same shape when installed
  * with no OS sandbox available the run fails closed unless the caller opts
    into `allow_unsandboxed` (recorded in results)
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from qwenbench.paths import repo_root, state_dir

ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "USER", "LOGNAME", "SHELL")

DENY_PATTERNS: list[tuple[str, str]] = [
    (r"(^|[;&|]\s*|\s)sudo\s", "sudo is not allowed"),
    (r"\bgit\s+(push|remote|fetch|pull|clone|config\s+--global|credential)\b", "git network/config operations are not allowed"),
    (r"\bgit\s+(commit|reset|rebase|stash|clean|checkout\s+--|restore|switch|branch\s+-[dD]|tag|am|cherry-pick|merge)\b",
     "git history/worktree-rewriting operations are not allowed; the harness captures your diff"),
    (r"\b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b", "piping downloads into a shell is not allowed"),
    (r"\b(ssh|scp|sftp|rsync|nc|ncat|telnet)\s", "remote shells and transfers are not allowed"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*(/|~|\$HOME)(\s|$|/\*)", "deleting / or home is not allowed"),
    (r"\b(mkfs|dd\s+if=|shutdown|reboot|halt|launchctl|systemctl)\b", "system administration commands are not allowed"),
    (r":\(\)\s*\{", "fork bombs are not allowed"),
    (r"\bkill\s+(-9\s+)?-1\b", "killing all processes is not allowed"),
    (r"\b(docker|podman|kubectl)\b", "container/cluster tooling is not available to the agent"),
    (r"\bqwen(bench)?\s+(up|down|override|hero|dispatch|guard)\b", "the agent may not drive its own infrastructure or routing"),
    (r"\bchmod\s+(-R\s+)?[0-7]*777\s+/", "chmod 777 on system paths is not allowed"),
]


def check_command(command: str) -> str | None:
    """Return a refusal reason, or None if the command passes static policy."""
    for pattern, reason in DENY_PATTERNS:
        if re.search(pattern, command):
            return reason
    return None


@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    output: str
    duration_s: float
    timed_out: bool = False
    refused: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.refused


def _real(p: Path) -> str:
    return str(Path(os.path.realpath(p)))


def _sb_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sensitive_read_paths() -> list[str]:
    home = Path.home()
    paths = [home / d for d in (".ssh", ".aws", ".gnupg", ".config/gh", ".docker", ".kube", ".netrc",
                                ".claude", ".claude.json", ".anthropic", ".runpod", ".cache/huggingface/token",
                                "Library/Keychains", ".password-store")]
    with contextlib.suppress(RuntimeError):
        paths.append(repo_root() / ".env")
    # Session files hold endpoint bearer tokens; overrides must not be forgeable.
    paths += [state_dir() / "sessions", state_dir() / "overrides", state_dir() / "events.jsonl"]
    return [_real(p) for p in paths]


def seatbelt_profile(workspace: Path, scratch: Path, network: bool, extra_writable: list[Path],
                     protected: list[Path] | None = None) -> str:
    writable = [_real(workspace), _real(scratch)] + [_real(p) for p in extra_writable]
    lines = ["(version 1)", "(allow default)"]
    if not network:
        lines += ["(deny network-outbound (remote ip))", '(allow network-outbound (remote ip "localhost:*"))']
    lines.append("(deny file-write*)")
    allow = " ".join(f"(subpath {_sb_quote(w)})" for w in writable)
    lines.append(f'(allow file-write* {allow} (literal "/dev/null") (literal "/dev/zero") '
                 '(subpath "/dev/fd") (regex #"^/dev/tty") (regex #"^/dev/ptmx") (regex #"^/dev/ttys"))')
    if protected:
        deny_write = " ".join(f"(subpath {_sb_quote(_real(p))})" for p in protected)
        lines.append(f"(deny file-write* {deny_write})")
    deny_read = " ".join(f"(subpath {_sb_quote(p)})" for p in sensitive_read_paths())
    lines.append(f"(deny file-read* {deny_read})")
    return "\n".join(lines) + "\n"


def detect_backend() -> str:
    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        return "macos-seatbelt"
    if system == "Linux" and shutil.which("bwrap"):
        return "linux-bwrap"
    return "none"


@dataclass
class Sandbox:
    workspace: Path
    scratch: Path
    network: bool = False
    allow_unsandboxed: bool = False
    extra_env: dict[str, str] = field(default_factory=dict)
    extra_writable: list[Path] = field(default_factory=list)
    protected: list[Path] = field(default_factory=list)   # never writable, even inside the workspace
    output_limit: int = 12_000
    backend: str = field(default_factory=detect_backend)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        self.scratch = Path(self.scratch).resolve()
        (self.scratch / "home").mkdir(parents=True, exist_ok=True)
        (self.scratch / "tmp").mkdir(parents=True, exist_ok=True)
        if self.backend == "none" and not self.allow_unsandboxed:
            raise RuntimeError(
                "no OS sandbox available (need macOS sandbox-exec or Linux bwrap). "
                "Install bubblewrap, or pass --allow-unsandboxed to accept the risk (recorded in results)."
            )
        if self.backend == "macos-seatbelt":
            self._profile = self.scratch / "sandbox.sb"
            self._profile.write_text(seatbelt_profile(self.workspace, self.scratch, self.network,
                                                      self.extra_writable, self.protected))

    def env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
        env.update({
            "HOME": str(self.scratch / "home"),
            "TMPDIR": str(self.scratch / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "CI": "1",
            "NO_COLOR": "1",
        })
        env.update(self.extra_env)
        return env

    def argv(self, command: str) -> list[str]:
        shell = ["/bin/bash", "-c", command]
        if self.backend == "macos-seatbelt":
            return ["sandbox-exec", "-f", str(self._profile), *shell]
        if self.backend == "linux-bwrap":
            args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                    "--bind", str(self.workspace), str(self.workspace),
                    "--bind", str(self.scratch), str(self.scratch), "--die-with-parent", "--new-session"]
            for p in self.extra_writable:
                args += ["--bind", str(p), str(p)]
            for p in self.protected:
                if p.exists():
                    args += ["--ro-bind", str(p), str(p)]
            for p in sensitive_read_paths():
                if os.path.isdir(p):
                    args += ["--tmpfs", p]
                elif os.path.exists(p):
                    args += ["--ro-bind", "/dev/null", p]
            if not self.network:
                args.append("--unshare-net")
            return args + ["--chdir", str(self.workspace), *shell]
        return shell

    def run(self, command: str, timeout_s: float = 120, check_policy: bool = True) -> CommandResult:
        if check_policy:
            refusal = check_command(command)
            if refusal:
                return CommandResult(command, None, f"REFUSED by sandbox policy: {refusal}", 0.0, refused=refusal)
        t0 = time.monotonic()
        proc = subprocess.Popen(self.argv(command), cwd=self.workspace, env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            timed_out = True
        text = out.decode("utf-8", "replace")
        return CommandResult(command, None if timed_out else proc.returncode, self.truncate(text),
                             round(time.monotonic() - t0, 3), timed_out=timed_out)

    def truncate(self, text: str) -> str:
        if len(text) <= self.output_limit:
            return text
        head = self.output_limit // 5
        tail = self.output_limit - head
        return f"{text[:head]}\n...[{len(text) - self.output_limit} chars truncated]...\n{text[-tail:]}"
