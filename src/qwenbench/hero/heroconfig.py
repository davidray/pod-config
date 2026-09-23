"""Read/modify a project's Hero configuration without clobbering it.

Mirrors Hero v0.34.1 semantics (internal/config/config.go): `.hero/hero.json`
is the committed base, `.hero/hero.local.json` is git-ignored and deep-merged
on top; for `models`, `default_model` is replaced when set and `roles` are
merged key by key.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class HeroModels:
    roles: dict[str, str]
    default_model: str | None

    def model_for(self, role: str) -> str | None:
        return self.roles.get(role) or self.default_model


def hero_dir(project: Path) -> Path:
    return project / ".hero"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def read_base(project: Path) -> dict[str, Any]:
    return _read_json(hero_dir(project) / "hero.json")


def read_local(project: Path) -> dict[str, Any]:
    return _read_json(hero_dir(project) / "hero.local.json")


def effective_models(project: Path) -> HeroModels:
    base = (read_base(project).get("models") or {})
    local = (read_local(project).get("models") or {})
    roles = dict(base.get("roles") or {})
    roles.update({k: v for k, v in (local.get("roles") or {}).items() if v})
    default = local.get("default_model") or base.get("default_model") or None
    return HeroModels(roles=roles, default_model=default)


def with_model_roles(doc: dict[str, Any], roles: dict[str, str]) -> dict[str, Any]:
    """Return a copy of `doc` with only models.roles[...] changed."""
    new = json.loads(json.dumps(doc))
    models = new.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError("existing `models` key is not an object; refusing to modify")
    existing = models.setdefault("roles", {})
    if not isinstance(existing, dict):
        raise ValueError("existing `models.roles` is not an object; refusing to modify")
    existing.update(roles)
    return new


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2) + "\n"


def unified_diff(path: Path, old: dict[str, Any], new: dict[str, Any]) -> str:
    old_text = render(old) if old else ""
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            render(new).splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
