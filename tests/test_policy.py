import pytest

from qwenbench.hero.heroconfig import HeroModels, effective_models
from qwenbench.hero.policy import normalize_agent, resolve, route_table

CONFIGURED = HeroModels({"design": "claude-opus-5-5", "execution": "qwen:a6000", "review": "claude-opus-5-5"}, None)

# Agent inventory installed by hero v0.34.1 (from a real project's .hero/install-state.json).
HERO_0_34_1_AGENTS = [
    "api-engineer", "architecture-reviewer", "brownfield-architect", "comment-scrubber", "convention-author",
    "database-engineer", "deadcode-scrubber", "debug-investigator", "dedup-scrubber", "defensive-scrubber",
    "dependency-analyst", "dependency-scrubber", "design-reviewer", "devops-engineer", "documentation-engineer",
    "engineer", "feature-delivery-lead", "functional-qa-engineer", "greenfield-architect", "integration-engineer",
    "issue-tracker", "legacy-scrubber", "migration-engineer", "performance-engineer", "platform-delivery-lead",
    "pr-reviewer", "product-ideator", "project-context-builder", "release-engineer", "roadmap-reviewer",
    "security-reviewer", "session-primer", "test-architect", "type-scrubber", "ui-designer",
]


@pytest.mark.parametrize("agent", ["engineer", "api-engineer", "database-engineer", "integration-engineer",
                                   "migration-engineer", "defensive-scrubber", "legacy-scrubber", "test-architect",
                                   "hero:engineer"])
def test_implementation_roles_route_to_qwen(cfg, agent):
    r = resolve(cfg.policy, agent, CONFIGURED)
    assert r.is_qwen and r.profile == "a6000" and r.hero_role == "execution"


@pytest.mark.parametrize("agent", ["product-ideator", "feature-delivery-lead", "platform-delivery-lead",
                                   "brownfield-architect", "greenfield-architect", "architecture-reviewer",
                                   "design-reviewer", "roadmap-reviewer", "security-reviewer"])
def test_judgment_roles_route_to_claude(cfg, agent):
    r = resolve(cfg.policy, agent, CONFIGURED)
    assert r.is_frontier and r.hero_role in ("design", "review")


def test_every_installed_hero_agent_is_classified(cfg):
    classified = set(cfg.policy.agents) | set(cfg.policy.builtin_agents)
    assert not set(HERO_0_34_1_AGENTS) - classified


def test_unknown_agent_fails_closed(cfg):
    r = resolve(cfg.policy, "shiny-new-agent", CONFIGURED)
    assert r.provider == "deny" and "not classified" in r.reason


def test_general_purpose_is_a_loophole_and_denied(cfg):
    assert resolve(cfg.policy, "general-purpose", CONFIGURED).provider == "deny"
    assert resolve(cfg.policy, "Explore", CONFIGURED).is_frontier


def test_unconfigured_hero_role_denies_instead_of_guessing(cfg):
    r = resolve(cfg.policy, "engineer", HeroModels({}, None))
    assert r.provider == "deny" and "no model configured" in r.reason


def test_default_model_applies_when_role_missing(cfg):
    r = resolve(cfg.policy, "engineer", HeroModels({"design": "claude-x"}, "qwen:l40s"))
    assert r.is_qwen and r.profile == "l40s"


def test_plain_qwen_routes_to_any_profile(cfg):
    r = resolve(cfg.policy, "engineer", HeroModels({"execution": "qwen"}, None))
    assert r.is_qwen and r.profile is None


def test_foreign_model_denied(cfg):
    r = resolve(cfg.policy, "engineer", HeroModels({"execution": "gpt-9"}, None))
    assert r.provider == "deny" and "matches no provider" in r.reason


def test_normalize():
    assert normalize_agent("hero:sub:engineer") == "engineer"


def test_local_overlay_merges_per_role(hero_project):
    import json
    (hero_project / ".hero" / "hero.json").write_text(json.dumps(
        {"models": {"roles": {"design": "claude-opus-5-5", "execution": "claude-sonnet-5"}}}))
    (hero_project / ".hero" / "hero.local.json").write_text(json.dumps({"models": {"roles": {"execution": "qwen:l40s"}}}))
    m = effective_models(hero_project)
    assert m.roles == {"design": "claude-opus-5-5", "execution": "qwen:l40s"}


def test_route_table_covers_everything(cfg):
    names = {r.agent for r in route_table(cfg.policy, CONFIGURED)}
    assert {"engineer", "brownfield-architect", "general-purpose"} <= names
