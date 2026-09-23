---
title: Qwen Runpod Bench
slug: qwen-runpod-bench
type: feature
status: planning
created: 2026-09-23
tags: [runpod, qwen, vllm, benchmark, hero-routing, claude-code]
---
# Qwen Runpod Bench

## Goal

Decide, with reproducible measurements, whether Runpod-hosted
Qwen3-Coder-30B-A3B-Instruct-FP8 can replace frontier models for routine
implementation work, while Claude keeps design, architecture and review. It
must also enforce that split in Hero-managed projects, with no silent fallback
to Claude.

## Background

Local Qwen is not viable on the owner's hardware, and Claude is already well
understood, so the open question is Runpod Qwen's quality, speed and cost on
real repo tasks, comparing A6000 and L40S. Hero exposes design, execution and
review model roles, but only as hints, and has no agent-to-role mapping
(verified against v0.34.1). Claude Code cannot serve a subagent from an
OpenAI-compatible endpoint.

## Design

See `docs/architecture.md` and ADRs 0001-0011. In summary:

- **Compute.** `ComputeProvider` with a Runpod Pods implementation over REST v2 (v1 retires 2026-11-15). Pods and volumes are discovered by name. Each profile (`a6000`, `l40s`) gets a network-volume model cache, since no data center hosts both GPUs with volumes.
- **Serving.** A pinned `vllm/vllm-openai:v0.30.0-cu129` image, used unmodified. A stdlib supervisor ships as a deterministic env-var bootstrap. It reports boot phases and runs the idle, startup, session and spend watchdog, which terminates its own pod. A local guard is the backstop.
- **Readiness.** An explicit FSM. READY requires a real authenticated completion from the expected served model.
- **Metrics.** A streaming instrumented OpenAI client records TTFT, tokens, cached tokens, retries and failures to JSONL.
- **Agent.** A minimal tool-calling agent loop in an OS sandbox (seatbelt or bwrap), plus the versioned `qwenbench.dispatch/v1` protocol.
- **Benchmark.** daveeval: declarative cases, isolated deterministic workspaces, hidden and restored validation, attempts with feedback, interventions, diff capture, reports, a comparability-checked compare, and label-carrying cost accounting.
- **Hero routing.** `role-policy.yaml` maps agent → Hero role, and Hero's `models.roles` maps role → model. Claude Code hooks deny Qwen-role subagents and Claude-side implementation edits. `qwenbench dispatch` is the only path for Qwen roles and fails fast with no fallback. A ledger with blob attribution backs `qwenbench hero audit`. Overrides are human-only and TTY-gated.

## Changes

- `config/`: models, runpod profiles, pricing, role policy
- `containers/qwen-vllm/`: entrypoint, supervisor, optional Dockerfile
- `infra/runpod/rendered/`: generated, drift-tested pod specs
- `src/qwenbench/`: cli, runpod, providers, agent, bench, hero, metrics
- `benchmarks/`: daveeval suite, six cases with reference solutions, ledgerlite fixture
- `docs/`, `docs/adr/`, `tests/`, `Makefile`, CI

Phases, each runnable and tested:

1. Skeleton, config, Runpod lifecycle, vLLM container, health/readiness, chat
2. Metrics, benchmark harness, worktree isolation, A6000/L40S comparison
3. Qwen coding agent and structured task protocol
4. Hero model mapping, per-agent policy, Claude Code enforcement
5. Reports, costs, idle shutdown and spend guards, docs

## Acceptance Criteria

- [ ] `qwenbench up a6000` / `qwenbench up l40s` reach verified READY on real Runpod and print phase timings (needs credentials; not yet run)
- [x] `qwenbench down --all` is idempotent and touches only `qwenbench-*` pods (tested against a fake v2 API)
- [x] Idle, startup, session and spend shutdowns terminate the pod and record the reason (supervisor tested as a real process; guard decisions unit-tested)
- [x] Same model revision, runtime, generation, agent, prompts, starting commits and validation on both GPUs; `compare` flags any difference
- [x] Every daveeval case: the base fails validation and the reference passes (`qwenbench bench verify-cases`)
- [x] A GPU-free end-to-end benchmark run produces run.json, requests.jsonl, per-trial result/diff, report.md and tasks.csv, with no secrets in results
- [x] `qwenbench hero configure` preserves unrelated Hero config, touches no Hero-owned file, and survives a forced `hero install`; `hero models --check` and `hero check` pass
- [x] Hooks deny Qwen-role subagents, Claude-side implementation edits, shell writes and self-granted overrides; dispatch fails fast with no fallback and caps attempts
- [ ] First live Claude Code session in a configured project confirms Claude follows the denial and dispatches (needs a logged-in `claude`)
