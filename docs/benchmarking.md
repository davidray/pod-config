# Benchmarking (daveeval)

## What a run does

For each trial and each case:

1. **Workspace.** A temp clone or copy of the case source at a deterministic starting commit. The source is never modified.
2. **Setup.** Trusted repo commands, such as installing dependencies, with network allowed and secrets scrubbed from the environment.
3. **Agent loop.** Qwen with tools, in the OS sandbox: no network, writes limited to the workspace. It sees the prompt, acceptance criteria and `agent_validation` commands.
4. **Validation.** Run by the harness, not the model:
   - paths in `validation.restore` are reset, so the agent cannot weaken tests
   - hidden tests from `validation.overlay` are copied in
   - `validation.commands` run in the sandbox
5. **Feedback.** If validation fails and attempts remain (`max_attempts`, default 2), the failure output is fed back in a fresh attempt. Success on attempt 1 with no interventions counts as **first-pass**.
6. **Record.** The diff (without the hidden tests), changed files, lines, every request and tool call, and timings are recorded, then the workspace is deleted (`--keep-workspaces` keeps it).

## The daveeval suite

| Case | Category | What it tests |
|---|---|---|
| `fix-pagination-regression` | fix a failing test | a seeded failing test plus user report; the hidden edge-case tests prevent test-specific hacks |
| `csv-export-feature` | feature from spec | written spec, registry extension point, RFC 4180 details |
| `refactor-discount-strategies` | refactor | behavior-preserving extraction; original tests restored; a grep check forbids leftover branching |
| `tax-rounding-multifile-bug` | multi-file bug | an accounting report with two root causes in two modules, and an existing test encoding the bug |
| `aging-report-tests` | add tests | coverage ≥95% **and** a mutation check (boundary/filter mutants must be killed) |
| `cli-customer-filter` | exploration | a cross-cutting option through CLI → repository with exact error contract |

All run against `benchmarks/fixtures/ledgerlite`, a small but realistic
invoicing library. `qwenbench bench verify-cases` proves each case is meaningful:
the starting state **fails** validation and the committed `reference.patch`
**passes**.

## Adding your own real-world cases

```
benchmarks/tasks/<id>/
  case.yaml     # schema: src/qwenbench/bench/suite.py
  prompt.md     # what you would tell an engineer
  hidden/       # optional tests copied in only for validation
  reference.patch  # optional; enables verify-cases
```

```yaml
id: api-rate-limit-headers
title: Add rate-limit headers to the public API
category: feature-from-spec
source:
  git: /Users/you/code/some-service    # or https://..., cloned, never mutated
  ref: 3f2c1a9                          # REQUIRED for git sources (comparability)
setup:
  - bundle install --quiet
task:
  role: api-engineer
  prompt_file: prompt.md
  acceptance_criteria: ["X-RateLimit-* headers on every /v1 response"]
agent_validation:            # the agent sees and runs these
  - bundle exec rspec spec/requests
validation:                  # the harness decides with these
  restore: [spec/requests/rate_limit_spec.rb]
  overlay: hidden
  commands: [bundle exec rspec]
  timeout: 15m
limits: {timeout: 25m, max_iterations: 60}
```

Add the id to a suite in `benchmarks/suites/`. The agent sandbox has no
network access, so everything validation needs must be installed by `setup`.

## Reading the results

`results/<run-id>/`:

```
run.json          model revision, image, vLLM argv, generation, GPU, pod, data center, startup phases,
                  bench repo git SHA (+dirty), per-case prompt hash and starting commits, costs
requests.jsonl    one line per HTTP attempt: ttft_s, generation_s, total_s, input/output/cached tokens,
                  output_tokens_per_s, http_status, error, attempt, run/task/trial/attempt/iteration
tasks/<case>/trial-N/
  result.json     success, first_pass, attempts[*] (finish status, validation, iterations, tool calls),
                  interventions, files_changed, lines +/-, gpu_attributable_s, estimated cost
  diff.patch      the agent's change (hidden tests excluded)
  setup.log  attempt-K/{transcript.json, tools.jsonl, validation.log}
  subjective.json (optional, from `qwenbench bench evaluate`)
report.md  tasks.csv
```

Reading the numbers:

- **TTFT** is measured client-side through the proxy, so it includes network time. Compare medians and p90, not single requests.
- **Output tok/s** is output tokens ÷ (last token − first token) per request: decode speed, excluding prefill.
- **Cached input tokens** come from vLLM prefix caching (`--enable-prompt-tokens-details`). Agent loops resend a growing conversation, so a high ratio is expected and is a big part of why TTFT stays low.
- **Interventions** are recorded only in `--interactive` mode: a hint (another attempt) or a manual fix (revalidated). A task with an intervention never counts as first-pass.
- Failures are never averaged away. Every trial is listed individually, and aggregates sit beside them.

## Comparability

`qwenbench bench compare` refuses to call runs comparable if any of these differ:

- model revision, max context, image or vLLM argv
- hardware overrides or generation parameters
- agent git SHA, prompt hashes or starting commits
- validation or max attempts

It also flags a dirty checkout. Commit before a real comparison run.
Sampling uses Qwen's recommended settings with a fixed seed. vLLM seeding makes
runs controlled, but not bit-identical across GPUs (different kernels), which
is why `--repeat 3` exists.

## Subjective scores

`qwenbench bench evaluate RUN CASE --trial N` records, on a 1-5 scale:

- correctness
- code quality
- adherence to existing architecture
- unnecessary changes
- babysitting required

plus free-text notes. They go in `subjective.json` and a separate report
section. They are never blended into objective metrics.

## First real measurement (2026-09-23, smoke suite, RTX A6000 Secure, EU-SE-1)

One trial, not a benchmark result, but it shows the system working end to end:

| | |
|---|---|
| Startup (request → verified inference) | 470 s, ephemeral storage (image pull + 31 GB download) |
| Task | `fix-pagination-regression`: PASS, first attempt, 49 s |
| Requests / output tokens | 22 / 2,473 (0 failed, 0 retries) |
| Input tokens | 101,059, of which 95,600 served from prefix cache |
| Median TTFT / p90 | 0.59 s / 1.28 s (through Runpod's proxy) |
| Median decode speed | 115 tok/s |
| GPU lifetime / cost | 8m41s / $0.077 (observed $0.53/hr) |

Qwen's fix used `(n + per_page - 1) // per_page`, equivalent to the reference solution.

## Full daveeval, both GPUs (2026-09-24, repeat 3, max 2 attempts, Secure Cloud, ephemeral storage)

Runs `20260924T020758Z-l40s-daveeval` and `20260924T030625Z-a6000-daveeval`.
`bench compare` found no comparability differences: same model revision,
runtime, generation settings, agent, prompts, starting commits and validation.

| metric | L40S | RTX A6000 |
|---|---|---|
| tasks passed (of 18) | 16 | 17 |
| first-attempt passes | 13 | 15 |
| startup to verified inference | 156 s | 445 s |
| benchmark wall time | 38m51s | 47m03s |
| median TTFT / p90 | 0.71 s / 1.43 s | 0.46 s / 0.80 s |
| median decode speed | 125 tok/s | 106 tok/s |
| requests (failed / retried) | 706 (0 / 0) | 691 (0 / 0) |
| GPU lifetime | 41m30s | 54m30s |
| GPU cost (observed rate) | $0.75 | $0.48 |
| cost per passed task | $0.047 | $0.028 |

Per case (passes of 3):

| case | L40S | A6000 |
|---|---|---|
| fix-pagination-regression | 3 | 3 |
| refactor-discount-strategies | 3 | 3 |
| tax-rounding-multifile-bug | 3 | 3 |
| csv-export-feature | 3 | 3 |
| cli-customer-filter | 3 (2 needed a second attempt) | 3 |
| aging-report-tests | 1 | 2 (both on the second attempt) |

Findings:

- Bug fixes, refactors and small features pass reliably on either GPU. The
  difference in pass counts is within noise at n=3; the model is the same.
- `aging-report-tests` is the weak spot, and it is a real model limitation, not a
  harness artifact. Every failure is the same surviving mutant (`result = result
  + amount` → `result = amount` in `total_outstanding()`): Qwen's tests never
  total two or more invoices. The second attempt was shown that exact surviving
  mutant and still did not add such a test. Treat Qwen-written tests (the
  `test-architect` role) as needing review, or keep that role on Claude.
- The L40S decodes about 18% faster, and it started faster in this run, though
  startup mostly depends on the host's image and model download speed. The A6000
  had lower TTFT. The A6000 is about 40% cheaper per passed task and was harder
  to get: it had no capacity for about 50 minutes, while an L40S was available
  immediately.
- Prefix caching covered about 98% of input tokens on both GPUs.
- Costs are observed-rate estimates. Runpod billing had not yet posted when the
  runs ended.
