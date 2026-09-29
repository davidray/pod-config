# qwenbench

Reproducible infrastructure and measurement for one question:

> **Can Runpod-hosted Qwen3-Coder do routine implementation work well enough
> and fast enough, while Claude keeps the higher-level engineering work?**

This repository provisions `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` on a Runpod
**RTX A6000** or **L40S** with pinned vLLM. It benchmarks both GPUs on real
repository coding tasks (not HumanEval puzzles), and wires the endpoint into
[Hero](https://heroengine.ai) projects. There, Claude Code is prevented, by
hooks rather than by advice, from doing Qwen-assigned implementation work
itself or silently falling back to Claude.

```
qwenbench up            -> first GPU with stock (a6000, a40, l40s) + in-pod idle watchdog + local guard
qwenbench chat a6000    -> OpenAI-compatible endpoint, per-response TTFT / tok/s
qwenbench bench run     -> real repo tasks in isolated workspaces, sandboxed agent, hidden validation
qwenbench bench compare -> A6000 vs L40S: successes, first-pass, TTFT, tok/s, GPU time, $ per success
qwenbench hero configure-> design/review -> Claude, execution -> Qwen, enforced by Claude Code hooks
qwenbench down --all    -> nothing left billing
```

## Zero to first inference

The command is `qwenbench`, not `qwen`: Alibaba's Qwen Code CLI installs a `qwen`
executable, and a PATH collision would be dangerous here, because Claude Code hooks call this tool.

Prerequisites: `git`, [`uv`](https://docs.astral.sh/uv/), a Runpod account
with credit. macOS gives the agent sandbox for free (`sandbox-exec`); on Linux
install `bubblewrap`. Hero and Claude Code are needed only for the routing part.

```bash
git clone git@github.com:davidray/pod-config.git qwenbench && cd qwenbench
make setup                 # uv sync, installs `qwenbench` on PATH, creates .env (chmod 600)
$EDITOR .env               # RUNPOD_API_KEY=... (HF_TOKEN optional: the model is not gated)
qwenbench doctor                # tools, credentials, config, live GPU availability, volumes
qwenbench up a6000              # first boot downloads ~31 GB into the cache volume (~10-20 min)
qwenbench chat a6000
qwenbench down a6000
```

`qwenbench up` prints each startup phase with timings and only reports ready
after a real authenticated completion from the expected model. Runpod saying
`RUNNING` is not enough:

```
pod requested -> pod allocated -> container started -> vLLM process started
              -> model loaded (/health + /v1/models) -> endpoint ready (inference verified)
```

## Commands

| | |
|---|---|
| `qwenbench setup` / `qwenbench doctor [--profile P] [--project DIR]` | first-run setup; full health check including endpoint identity and Hero |
| `qwenbench up [P] [--idle-timeout 60m\|off] [--max-session 4h] [--max-spend 5] [--storage network-volume]` | provision and wait for verified readiness |
| `qwenbench status [P]` | live pods, $/hr, spend so far, idle clock, guard, recent auto-shutdowns |
| `qwenbench endpoint P [--export] [--show-key]` / `qwenbench logs P [-f]` / `qwenbench chat P` | use the endpoint |
| `qwenbench down P` / `qwenbench down --all` | terminate (idempotent) |
| `qwenbench list` | profiles and any live qwenbench pods |
| `qwenbench keepalive P` | reset the idle clock without inference |
| `qwenbench storage list\|ensure P\|delete P` | persistent model-cache volumes |
| `qwenbench infra render [--check]` / `qwenbench infra show P` | the committed infrastructure definition |
| `qwenbench bench run -p P -s daveeval [--repeat 3] [--case ID] [--interactive] [--auto-up --down-after]` | benchmark |
| `qwenbench bench report RUN [--format md\|csv\|json]` / `qwenbench bench compare RUN_A RUN_B` | results |
| `qwenbench bench evaluate RUN CASE [--trial N]` | your 1-5 subjective scores (stored separately) |
| `qwenbench bench finalize RUN` | pull authoritative Runpod billing once it has settled |
| `qwenbench bench verify-cases` | GPU-free self-test: every case's base fails, its reference solution passes |
| `qwenbench hero inspect\|configure\|verify\|audit\|unconfigure DIR` | Hero/Claude Code routing |
| `qwenbench dispatch --project DIR --role R --task-file F --validate CMD` | run a Qwen-assigned role (what Claude calls) |
| `qwenbench override grant\|list\|revoke` | human-only escape hatch |

## Benchmarking A6000 vs L40S

```bash
qwenbench up a6000
qwenbench bench run --profile a6000 --suite daveeval --repeat 3
qwenbench down a6000

qwenbench up l40s
qwenbench bench run --profile l40s --suite daveeval --repeat 3
qwenbench down l40s

qwenbench bench list
qwenbench bench compare <a6000-run-id> <l40s-run-id>
```

Or one command per GPU: `qwenbench bench run -p l40s -s daveeval --repeat 3 --auto-up --down-after`.

Both profiles run the identical model revision, image, vLLM argv, generation
settings, agent loop, prompts, starting commits and validation. `compare`
checks this and prints **NOT directly comparable** with the reasons if not.
See [docs/benchmarking.md](docs/benchmarking.md).

## Hero integration

```bash
qwenbench up                                                          # first of a6000, l40s with capacity
qwenbench hero inspect ~/code/myproject                               # roles, agents, routes (read-only)
qwenbench hero configure ~/code/myproject                             # shows the diff, asks, applies
qwenbench hero verify ~/code/myproject                                # proves the wiring, incl. a real hook call
# work in Claude Code as usual; afterwards:
qwenbench hero audit ~/code/myproject                                 # which model did what
```

[docs/hero-routing.md](docs/hero-routing.md) explains exactly what is
enforced, how, and where the limits are.

## Safety against cloud spend

- At most one GPU at a time by default (`guards.max_gpu_count: 1`). A second profile is refused while the first is live.
- In-pod watchdog: idle 30 min (configurable per `up`), startup timeout 25 min, max session 8 h, max spend $10. It terminates its own pod and records the reason on the volume.
- Local guard (`qwenbench guard`, started by `up`) is a second layer. It uses your account key and catches a watchdog that cannot stop its pod.
- A failed startup terminates the pod (use `--keep-on-failure` to debug).
- `qwenbench status` shows estimated spend and warns about any live pod. `qwenbench down --all` terminates everything qwenbench created, and nothing else.
- Network volumes are billed monthly whether or not a GPU runs: [docs/COSTS.md](docs/COSTS.md).

## Tests

```bash
make test        # ~20 s; never spends money. Includes a GPU-free end-to-end benchmark
                 # run against a scripted fake Qwen server and the real sandbox.
QWEN_RUNPOD_INTEGRATION=readonly QWEN_IT_REAL_KEY=... uv run pytest tests/test_integration_runpod.py
QWEN_RUNPOD_INTEGRATION=1        QWEN_IT_REAL_KEY=... uv run pytest tests/test_integration_runpod.py  # PAID
```

## Layout

```
config/            models.yaml runpod.yaml pricing.yaml role-policy.yaml   <- all tunables
containers/qwen-vllm/  entrypoint.sh supervisor.py Dockerfile             <- in-pod code
infra/runpod/rendered/ <profile>.json                                     <- generated IaC, drift-tested
benchmarks/        suites/ tasks/<case>/ fixtures/ledgerlite/             <- daveeval
src/qwenbench/     cli/ runpod/ providers/ agent/ bench/ hero/ metrics/
docs/              architecture, benchmarking, hero-routing, runpod-setup, COSTS, agent-protocol, adr/
tests/
```

## Documentation

- [docs/architecture.md](docs/architecture.md): components and data flow
- [docs/runpod-setup.md](docs/runpod-setup.md): account, key, storage, startup, cleanup
- [docs/benchmarking.md](docs/benchmarking.md): writing cases, running, reading metrics
- [docs/hero-routing.md](docs/hero-routing.md): model policy, enforcement, override procedure
- [docs/agent-protocol.md](docs/agent-protocol.md): the dispatch contract
- [docs/COSTS.md](docs/COSTS.md): GPU vs storage costs, estimated vs authoritative
- [docs/adr/](docs/adr/): architecture decisions
