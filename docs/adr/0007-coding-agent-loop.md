# ADR 0007: A small in-repo coding-agent loop

Status: accepted (2026-09-23)

## Options
| Option | Pros | Cons |
|---|---|---|
| OpenHands | full-featured | very large; Docker-centric; its own runtime and metrics model |
| Aider | mature editing | edit-format driven, not tool-calling; hard to attribute tokens per step |
| Qwen Code CLI (Node) | tuned for Qwen3-Coder | large Node app; measuring would need a metering proxy; its own sandboxing story |
| mini-swe-agent | tiny, bash-only | pulls in litellm; no structured file tools; bash-only edits hurt small models |
| **In-repo loop** (~300 lines) | exact metrics per request and tool call; our sandbox; our protocol | we maintain it |

## Decision
Write the minimal loop (`agent/loop.py`):

- **Transport:** OpenAI tool calling (vLLM `qwen3_coder` parser), streamed.
- **Tools:** list_files, read_file (numbered), search, write_file, edit_file (exact unique match), run_command, finish.
- **Limits:** iteration, wall-clock and token budgets; elision of old tool output near the context limit; three idle turns count as no progress.
- **Validation** is done by the harness, never trusted from the model.

**Sandboxing.** Shell commands run under `sandbox-exec` on macOS or bubblewrap
on Linux:

- writes allowed only to the workspace and a private scratch dir
- outbound network denied, loopback allowed
- reads of ~/.ssh, ~/.aws, ~/.config/gh, keychains, the bench `.env` and qwenbench session/override state denied
- the environment rebuilt from an allowlist
- a static command policy on top (no git history rewrites or pushes, sudo, remote shells, or `qwen up/down/override`)

The seatbelt profile was verified on this machine; a profile containing
`(allow network* (remote unix-socket))` silently re-enabled egress, so it was
removed. The run fails closed with no OS sandbox unless `--allow-unsandboxed`
is given, and that is recorded.

## Consequences
The loop is deliberately simple. If the benchmark shows Qwen is limited by the
harness rather than the model, swapping in Qwen Code via the dispatch
protocol (ADR 0006, docs/agent-protocol.md) is the next experiment.
