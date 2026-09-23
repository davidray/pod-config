# ADR 0006: Enforcing the boundary in Claude Code

Status: accepted (2026-09-23)

## Options investigated, in the requested order
1. **Hero-native routing.** None exists (ADR 0005).
2. **Project instructions** (CLAUDE.md / CLAUDE.local.md). Useful for guidance, but not enforcement.
3. **Hooks.** PreToolUse receives `tool_name`, `tool_input` (`subagent_type` for the Agent tool) and `agent_type` when called inside a subagent. It can deny with exit 2 plus a `permissionDecision: "deny"` JSON (Claude Code hooks docs, 2026-09). SessionStart can inject context; Stop can surface a `systemMessage`.
4. **Wrapper/dispatcher.** Required, because a Claude Code subagent cannot be served by an OpenAI-compatible model.
5. **Intercepting generated Hero workflows.** Not needed; rejected because `hero upgrade` would undo it.

## Decision
Hooks (3) plus a dispatcher CLI (4):
- **Deny:** spawning Qwen-routed, unclassified or loophole (`general-purpose`) subagents.
- **Deny:** Claude-side edits to implementation files (spec/doc globs are allowed) and shell writes to them.
- **Deny:** `qwenbench override` from Claude.
- **Protect:** routing config.
- **Instruct:** every denial names the exact `qwenbench dispatch` command. `qwenbench dispatch` refuses frontier roles, fails fast on an unavailable endpoint, and caps attempts per task. There is no Claude fallback code path at all.
- **Audit:** content-level attribution (the ledger records blob SHAs; the Stop hook and `qwenbench hero audit`) catches anything the Bash heuristics miss.
- **Override:** human-only, TTY-gated, expiring and logged.

A CLI was chosen over an MCP server. It adds no dependency, long tasks can
run as background Bash commands, and exit codes plus JSON are unambiguous. An
MCP wrapper can be added on top of the same function if desired.

## Consequences
- The boundary holds for Claude Code sessions in configured projects. Other harnesses (Cursor, Codex) are not covered.
- Shell-write detection is best-effort by nature, which is why audit exists.
- Each governed tool call pays about 110 ms of hook latency (measured; mostly Python start-up).
