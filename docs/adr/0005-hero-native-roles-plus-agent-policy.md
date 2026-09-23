# ADR 0005: Hero's native model roles plus a thin per-agent policy

Status: accepted (2026-09-23)

## Context (verified against hero v0.34.1 source and a real install)
- **`models.roles`** has `design`, `execution` and `review`, plus `default_model`. `.hero/hero.local.json` overlays roles key by key and is git-ignored by `hero init`.
- **Roles are hints.** Hero passes them to agents as context and performs no routing.
- **Agent files carry no role tag.** Installed `.claude/agents/*.md` have no `role:` frontmatter, even though Hero's own design spec proposed it. There is no agent-to-role mapping anywhere in Hero.

## Decision
- **Hero stays the source of truth** for which model serves each role. `qwenbench hero configure` writes `design` and `review` as the frontier model and `execution` as `qwen:<profile>` into `hero.local.json`.
- **`config/role-policy.yaml` maps agent → Hero role**, the one thing Hero lacks. It can also map an agent directly to a provider (e.g. `Explore: frontier`) or to `deny`.
- **Unclassified agents are denied** (fail closed).

## Consequences
- No duplication: changing the Qwen GPU for a project is one Hero value.
- A future Hero release that adds `role:` frontmatter could replace the agent map; the policy file would then shrink to overrides.
- Hero-owned files are never edited, so `hero upgrade` cannot undo routing. We verified this with a forced reinstall.
