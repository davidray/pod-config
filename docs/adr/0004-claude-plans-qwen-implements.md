# ADR 0004: Claude plans, designs and reviews; Qwen implements

Status: accepted (2026-09-23)

## Context
Frontier-model value per token is highest for deciding *what* to build:
ideation, specification, architecture, cross-system planning, judgment-heavy
security review, and independent final review. Routine implementation is
token-heavy and well specified once a good spec exists.

## Decision
- **Claude** performs design and review roles: product-ideator, delivery leads, architects, UI designer, debug-investigator (root-cause classification), dependency analyst, all reviewers, and release engineering.
- **Qwen** performs implementation roles: the engineers, test-architect, functional QA, documentation/devops engineers, and all scrubbers.
- The full mapping is `config/role-policy.yaml`.

## Consequences
- Output quality depends on spec quality. Hero's design phase produces the context Qwen needs, and dispatch passes it explicitly (`context`, `acceptance_criteria`, `validation_commands`).
- Borderline roles (`performance-engineer`, `test-architect`, `documentation-engineer`, `security-reviewer`) are one line each to reclassify as evidence accumulates.
