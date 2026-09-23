"""Secret loading and redaction.

Secrets come only from the process environment or the git-ignored `.env` file in
the repo root. Anything written to disk (results, state, logs) passes through
`redact()` so a secret value can never be serialized, even if it ends up nested
inside an error message or a request echo.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from qwenbench.paths import repo_root

SECRET_NAMES = ("RUNPOD_API_KEY", "RUNPOD_SELF_STOP_API_KEY", "HF_TOKEN", "QWEN_ENDPOINT_API_KEY", "ANTHROPIC_API_KEY")
SECRET_KEY_PATTERN = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)
REDACTED = "***REDACTED***"

_dotenv_cache: dict[str, str] | None = None


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def dotenv() -> dict[str, str]:
    global _dotenv_cache
    if _dotenv_cache is None:
        try:
            _dotenv_cache = _parse_dotenv(repo_root() / ".env")
        except RuntimeError:
            _dotenv_cache = {}
    return _dotenv_cache


def get_secret(name: str) -> str | None:
    """Environment first, then .env. Empty strings count as unset."""
    value = os.environ.get(name) or dotenv().get(name)
    return value or None


def require_secret(name: str) -> str:
    value = get_secret(name)
    if not value:
        raise MissingSecret(name)
    return value


class MissingSecret(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"{name} is not set. Export it or add it to .env (see .env.example).")
        self.name = name


def known_secret_values(extra: list[str] | None = None) -> list[str]:
    values = [get_secret(n) for n in SECRET_NAMES]
    values += extra or []
    # Very short values would redact innocent substrings; real keys are long.
    return sorted({v for v in values if v and len(v) >= 8}, key=len, reverse=True)


def redact(obj: Any, extra_secrets: list[str] | None = None) -> Any:
    """Recursively scrub secret values and secret-looking keys from a JSON-able object."""
    secrets = known_secret_values(extra_secrets)
    return _redact(obj, secrets)


def _redact(obj: Any, secrets: list[str]) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and SECRET_KEY_PATTERN.search(k) and isinstance(v, str) and v:
                out[k] = REDACTED
            else:
                out[k] = _redact(v, secrets)
        return out
    if isinstance(obj, list | tuple):
        return [_redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            if s in obj:
                obj = obj.replace(s, REDACTED)
        return obj
    return obj
