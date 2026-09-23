# Architecture

```
                         config/*.yaml  (models, runpod profiles, pricing, role policy)
                                │
            ┌───────────────────┼─────────────────────────────┐
            ▼                   ▼                             ▼
   ComputeProvider        ModelProvider                  Role policy
   (runpod/provider.py)   (providers/openai_compat.py)   (hero/policy.py)
   up/down/status/logs    endpoint/health/probe          agent -> Hero role -> model id -> provider
            │                   │                             │
            ▼                   ▼                             ▼
   Runpod REST v2 ──► Pod: pinned vllm/vllm-openai image    Claude Code hooks (hero/hooks.py)
                      + supervisor.py (phases, idle/        deny Qwen roles as Claude subagents,
                        session/spend watchdog, /status)    deny Claude editing implementation files
                      + network volume (HF cache)                     │
            │                   ▲                                     ▼
            │                   │ OpenAI-compatible HTTPS    qwenbench dispatch (hero/dispatch.py)
            │                   │                                     │
            │           ChatClient (metrics/llm.py) ◄───── agent loop (agent/loop.py)
            │           TTFT, tokens, cache hits,          tools (agent/tools.py) in an OS
            │           retries -> requests.jsonl          sandbox (agent/sandbox.py)
            │                   ▲                                     ▲
            └── local guard     │                                     │
                (runpod/guard)  └──── benchmark harness (bench/runner.py) — same loop, isolated
                                      workspaces, hidden validation, reports, cost accounting
```

## Components

**Compute** (`src/qwenbench/runpod/`). `RunpodPodsProvider` implements the
`ComputeProvider` interface in `providers/base.py` over Runpod REST v2.
`podspec.py` renders a profile into the exact `vllm serve` argv and the
`CreatePodRequest`. `qwenbench infra render` commits that rendering, secrets
excluded, to `infra/runpod/rendered/`, and a test fails on drift.

**In-pod supervisor** (`containers/qwen-vllm/supervisor.py`). It is stdlib
only and runs inside the unmodified pinned upstream image. Its jobs:

- launch vLLM
- report boot phases
- enforce idle/startup/session/spend limits by terminating its own pod
- persist shutdown reasons on the volume
- serve a token-protected `/status`

It is delivered as a deterministic base64 tarball in an env var, so no
registry is needed ([ADR 0010](adr/0010-runpod-rest-v2-and-bootstrap-delivery.md)).

**Model access** (`providers/openai_compat.py`, `metrics/llm.py`). Everything
that talks to the model uses the instrumented `ChatClient`. It always streams,
which gives client-side TTFT and keeps Cloudflare's roughly 100 s proxy limit
at bay. It records usage, including vLLM's cached-token count, and every
retry.

**Agent** (`src/qwenbench/agent/`). A minimal tool-calling loop:

- **Tools:** list, read, search, write, edit, run, finish.
- **File tools** are path-confined in Python.
- **Shell commands** run through the OS sandbox: writes confined to the workspace, no network egress, no credential reads, a scrubbed environment and a command policy.
- **`protocol.py`** defines the versioned `DispatchRequest`/`DispatchResult` contract.
- **`snapshot.py`** diffs working trees as git tree objects without touching HEAD, refs or the index.

**Benchmark** (`src/qwenbench/bench/`). The flow is:

1. Declarative cases (`benchmarks/tasks/*/case.yaml`) run in throwaway workspaces with deterministic starting commits.
2. Setup runs, then the agent loop.
3. Validation runs in the sandbox after restoring protected files and overlaying hidden tests.
4. The diff is captured, and results are written as JSON/JSONL.

Reports and comparisons are generated from those files.

**Hero routing** (`src/qwenbench/hero/`):

| Module | Role |
|---|---|
| `heroconfig.py` | Mirrors Hero's `hero.json` + `hero.local.json` merge |
| `policy.py` | Resolves agent → role → model → provider |
| `hooks.py` | Implements the Claude Code hooks |
| `dispatch.py` | The only sanctioned way to perform a Qwen-assigned role |
| `ledger.py`, `audit.py` | Record and prove which model did what |
| `override.py` | Human-only exceptions |
| `configure.py` | Installs all of this without touching Hero-owned files |

## Extending

- **Another compute backend** (Runpod Serverless, a workstation, DGX Spark,
  another cloud): implement `ComputeProvider` and return an `Endpoint`.
  Benchmarks, dispatch and Hero routing depend only on `Endpoint`.
- **Another implementation model**: point a profile at it, or add a provider
  rule in `role-policy.yaml` (e.g. `local:(?P<profile>...)`). The dispatch
  protocol is model-agnostic.
- **Another agent**: anything that consumes a `DispatchRequest` and produces a
  `DispatchResult` can replace `agent/loop.py`.
