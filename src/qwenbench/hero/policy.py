"""Agent -> provider routing decisions.

Resolution chain (fail closed at every step):

    agent name --role-policy.yaml--> Hero role (design/execution/review)
               --project Hero models.roles--> model id (e.g. "qwen:a6000")
               --role-policy providers--> provider ("qwen" + profile, or "frontier")
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from qwenbench.config import HERO_ROLES, RolePolicyFile
from qwenbench.hero.heroconfig import HeroModels, effective_models

FRONTIER = "frontier"
DENY = "deny"


@dataclass(frozen=True)
class Route:
    agent: str
    provider: str  # "frontier" | "qwen" (provider key) | "deny"
    provider_type: str | None  # "claude" | "openai-compatible" | None
    hero_role: str | None
    model_id: str | None
    profile: str | None
    reason: str

    @property
    def is_qwen(self) -> bool:
        return self.provider_type == "openai-compatible"

    @property
    def is_frontier(self) -> bool:
        return self.provider_type == "claude"

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_agent(name: str) -> str:
    """'hero:engineer' / 'plugin:sub:engineer' -> 'engineer'."""
    return name.strip().rsplit(":", 1)[-1]


def match_provider(policy: RolePolicyFile, model_id: str) -> tuple[str, str, str | None] | None:
    for key, rule in policy.providers.items():
        m = re.search(rule.model_pattern, model_id)
        if m:
            profile = m.groupdict().get("profile") if m.groupdict() else None
            return key, rule.type, profile
    return None


def resolve(policy: RolePolicyFile, agent: str, models: HeroModels) -> Route:
    name = normalize_agent(agent)
    target = policy.agents.get(name) or policy.builtin_agents.get(name)
    if target is None:
        if policy.enforcement.unknown_agents == DENY:
            return Route(name, DENY, None, None, None, None,
                         f"agent {name!r} is not classified in config/role-policy.yaml (unknown_agents: deny)")
        target = FRONTIER
    if target == DENY:
        return Route(name, DENY, None, None, None, None,
                     f"agent {name!r} is denied by role policy (it could implement code outside the Qwen route)")

    hero_role = target if target in HERO_ROLES else None
    if hero_role:
        model_id = models.model_for(hero_role)
        if not model_id:
            return Route(name, DENY, None, hero_role, None, None,
                         f"Hero role {hero_role!r} has no model configured (models.roles.{hero_role}); "
                         "run `qwen hero configure`")
    else:
        # Direct provider mapping, e.g. `Explore: frontier`.
        rule = policy.providers.get(target)
        if rule is None:
            return Route(name, DENY, None, None, None, None, f"unknown provider {target!r}")
        model_id = None
        return Route(name, target, rule.type, None, None, None, f"{name} -> provider {target} (direct)")

    matched = match_provider(policy, model_id)
    if matched is None:
        return Route(name, DENY, None, hero_role, model_id, None,
                     f"model id {model_id!r} for Hero role {hero_role!r} matches no provider in role-policy.yaml")
    provider, ptype, profile = matched
    if ptype == "openai-compatible" and not profile:
        return Route(name, DENY, ptype, hero_role, model_id, None,
                     f"model id {model_id!r} does not name a compute profile (expected e.g. qwen:a6000)")
    return Route(name, provider, ptype, hero_role, model_id, profile,
                 f"{name} -> Hero role {hero_role} -> {model_id} -> provider {provider}")


def resolve_for_project(policy: RolePolicyFile, agent: str, project: Path) -> Route:
    return resolve(policy, agent, effective_models(project))


def route_table(policy: RolePolicyFile, models: HeroModels, extra_agents: list[str] | None = None) -> list[Route]:
    names = sorted(set(policy.agents) | set(policy.builtin_agents) | set(extra_agents or []))
    return [resolve(policy, n, models) for n in names]
