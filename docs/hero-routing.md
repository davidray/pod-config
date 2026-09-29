# Hero routing: Claude plans, Qwen implements

## What Hero provides natively (verified against hero v0.34.1)

- `models.roles` in `.hero/hero.json`, overlaid by the git-ignored `.hero/hero.local.json`. Roles are `design`, `execution` and `review`, plus `default_model`. `hero models [--check]` displays and validates them.
- These are **hints only**. From Hero's own help: "the actual model selection depends on your AI tool's configuration". Hero does no routing.
- Installed agents (`.claude/agents/*.md`) carry **no role tag**. Hero's design spec proposed `role:` frontmatter, but v0.34.1 agents do not have it. Nothing in Hero maps `engineer` to `execution`.
- Hero owns `.claude/agents`, `.claude/commands`, `.claude/skills`, the fenced block in `CLAUDE.md`, and its `added_by_hero` entries in `.claude/settings.json`. The list is in `.hero/install-state.json`, and `hero upgrade`/`install` rewrites these files.

So qwenbench keeps Hero's three roles as the source of truth for **which
model serves a kind of work**. `config/role-policy.yaml` adds only the missing
piece: **which agent does which kind of work**.

```
engineer --role-policy.yaml--> execution --hero.local.json--> "qwen" --providers--> Qwen on whichever profile is ready
brownfield-architect --------> design    --------------------> "claude-opus-5-5" -> Claude (native)
```

By default the execution role maps to plain `qwen`: dispatch uses whichever
profile is ready, checked in `profile_preference` order (`config/runpod.yaml`),
and `qwenbench up` with no profile starts the first one with capacity. Pin a
project to one GPU with `qwenbench hero configure DIR --execution-profile l40s`
(model id `qwen:l40s`). Reclassify an agent in
`role-policy.yaml`. Agents that are not classified, such as a new agent added
by a future `hero upgrade`, are **denied** until you classify them;
`qwenbench hero inspect` lists them.

## What `qwenbench hero configure` changes

Always with a diff first, and a backup (`*.qwenbench.bak`) of anything it overwrites:

| File | Change | Owner |
|---|---|---|
| `<git common dir>/qwenbench/config.json` | the binding: enforce flag, profile, roles, agent sandbox options. Every worktree of the repo sees it, and git never tracks it | qwenbench |
| `.hero/hero.local.json` | `models.roles.{design,review}` = frontier model, `execution` = `qwen` (or `qwen:<profile>`); every other key preserved | Hero's documented local overlay (git-ignored) |
| `.claude/settings.local.json` | removes per-checkout qwenbench hooks left by older versions; user entries preserved | yours |
| `CLAUDE.local.md` | a fenced `qwenbench:routing` block | yours |
| `.git/info/exclude` | keeps local files out of git without editing `.gitignore` | yours |

Nothing Hero owns is touched. A forced `hero install project . --target
claude --force` was run over a configured project in testing and left every
qwenbench file byte-identical. Then `hero models --check` and `hero check`
are run to validate the result with Hero's own tooling.

### The hooks are user-level (`qwenbench hero install-hooks`, once per machine)

Claude Code loads project hooks from a session's project root. The desktop app
starts each session in a fresh worktree, which has none of a checkout's
untracked files, so per-checkout hooks silently never ran in the first live
trial (see below and ADR 0006). The hooks therefore live in
`~/.claude/settings.json`, tagged `"added_by": "qwenbench"`, and call a small
gate script (`~/.local/state/qwenbench/claude-hook.sh`):

- **Where it acts:** the gate hands off to qwenbench only when the session's repository has a binding. It checks the event's `cwd` and `CLAUDE_PROJECT_DIR`. Anywhere else it exits after one `git` call, about 28 ms median per governed tool call, measured.
- **How it picks the project:** qwenbench judges the repository the session is working in right now, which is the event `cwd`, not where the session started. A file edit in another bound repository is judged by that repository.
- **What Claude can't touch:** the binding, `~/.claude/settings.json` and the gate script are protected. `qwenbench hero configure|unconfigure|install-hooks|uninstall-hooks` and `qwenbench override` are denied to Claude.
- **Scope:** the binding covers **every checkout of the repository on this machine**. If you only want to route a trial, use a separate clone, not a worktree of your working repo.
- **Removing it:** `qwenbench hero uninstall-hooks` removes the hooks everywhere; `qwenbench hero unconfigure DIR` stops enforcement for one repository.

## How enforcement actually works

Claude Code cannot route a subagent to an OpenAI-compatible endpoint. A
subagent's `model:` selects among models reachable through Claude Code's own
provider (Anthropic, Bedrock, Vertex, Foundry). Pointing Claude Code at vLLM
would need an Anthropic-API translation proxy, and would also turn the
*orchestrator* into Qwen. So "Qwen performs the engineer role" cannot be a
Claude subagent setting. Instead:

1. **PreToolUse hook on `Agent`/`Task`.** Spawning a Qwen-routed agent such as `engineer` or `api-engineer` is **denied**. The denial message tells Claude exactly how to delegate: `qwenbench dispatch --project … --role … --task-file …`. The loophole agents (`general-purpose`, `claude`) and unclassified agents are denied too. Design and review agents are allowed.
2. **PreToolUse hook on `Edit`/`Write`/`MultiEdit`/`NotebookEdit`.** The main thread and design/review subagents (identified by the hook's `agent_type` field) may write only specs and docs (`.hero/**`, `docs/**`, `*.md`). Implementation files are denied, with the same instruction to dispatch. Routing configuration (`.claude/settings*.json`, `.hero/hero.local.json`, `.qwen-routing/**`) is protected from Claude entirely.
3. **PreToolUse hook on `Bash`.** It denies `qwenbench override …`, because overrides are human-only. It also denies shell writes into implementation files: redirects, `tee`, `sed -i`/`perl -i`, `git apply`/`am`, `patch`, `git checkout -- path`. Everything else, such as running tests or `qwenbench dispatch`, is allowed.
4. **`qwenbench dispatch`** runs the role on Qwen, in an OS sandbox confined to the project. Routing config inside the project is write-protected from the Qwen agent too. The call returns a JSON `DispatchResult` ([agent-protocol.md](agent-protocol.md)).
   - It **refuses** frontier roles (exit 3).
   - It **fails fast** if the endpoint is down or unhealthy (exit 4, `endpoint_unavailable`). It never falls back.
   - It stops after `max_attempts_per_task` dispatches of the same task (exit 5): "surface the failure to the human; do not implement it with Claude".
   - Validation is run by the harness. A model claiming success when tests fail is reported as `validation_failed`.
5. **SessionStart hook.** Injects the routing policy into Claude's context and snapshots the working tree as the session baseline.
6. **Stop hook and `qwenbench hero audit`.** Every file changed since the baseline must match, blob for blob, the content some Qwen dispatch produced. Anything else is flagged as **unattributed**. This catches edits the Bash heuristics could not see.

The hooks fail closed: if qwenbench's config cannot load, Agent and Edit calls
are denied.

## Proving which model did what

```bash
qwenbench hero audit ~/code/myproject          # or --json
```

- `.qwen-routing/ledger.jsonl` holds every dispatch: role, route, served model name observed in responses, profile, pod id, GPU, files and their blob SHAs, metrics.
- `.qwen-routing/decisions.jsonl` holds every hook decision: which Claude subagents ran, what was denied and why, overrides.
- `.qwen-routing/dispatches/<id>/` holds the full transcript, tool log, request metrics and diff for each dispatch.
- `.qwen-routing/baselines/<session>.json` holds the working tree when each Claude session first hit a hook here.

The audit labels every changed implementation file since the earliest baseline:
- `qwen`: the content matches a dispatch result.
- `claude-override`: Claude edited it through a tool call a human override allowed.
- `unattributed`: anything else.

Commits don't hide changes, because the comparison is against the baseline tree, not HEAD. The audit also says so plainly when **no Claude session ever ran the hooks here**. It excludes `qwenbench hero verify`'s own test calls, which use session id `qwen-hero-verify`. It lists each dispatch's warnings, such as removed assertions.

## Explicit human override

When you decide Claude should do Qwen-assigned work, for example because the
GPU is unavailable and the fix is urgent:

```bash
# in YOUR terminal (it refuses without a TTY and asks you to type the role name)
qwenbench override grant --project ~/code/myproject --role engineer --ttl 1h \
     --reason "Runpod out of A6000 capacity; hotfix"
qwenbench override grant --project ~/code/myproject --role '*' --scope edits --ttl 30m --reason "..."
qwenbench override list --project ~/code/myproject
qwenbench override revoke --project ~/code/myproject
```

- `--scope agent` lets Claude spawn that role as a Claude subagent.
- `--scope edits` lets Claude edit implementation files directly.

Overrides expire, live outside the project where agents cannot write them, and
are logged in both the project ledger and the global event log. Claude cannot
grant one: the Bash hook denies `qwenbench override`, and the command itself
requires an interactive TTY.

## Limits (honest)

- Enforcement needs the user-level hooks (`qwenbench hero install-hooks`). Without them nothing is enforced, and `qwenbench hero verify` fails. A session with no hooks is what the first live trial ran as; `qwenbench hero audit` reports it.
- Evidence (`.qwen-routing/`) is written in the checkout where the work happens, so audit each checkout you worked in.

- The Bash write detection is heuristic. A determined agent could write a file through, say, a Python one-liner. That is why the Stop hook and `qwenbench hero audit` check content attribution: such edits are reported, not silently accepted.
- Hooks govern Claude Code sessions in the configured project. Other tools such as Cursor or Codex are not covered.
- Hero workflows that tell Claude to spawn `engineer` get a denial whose reason spells out the `qwenbench dispatch` command, and CLAUDE.local.md plus the SessionStart context say the same. Hero's own prompt text is not modified, by design.
- The dispatch agent sandbox has no network by default. Enable it per project in `.qwen-routing/config.json` (`sandbox.network`, `sandbox.real_home`, `sandbox.extra_writable`) if your tests need it.

## First live trial (2026-09-24, bookwyrm-editor `chapter-word-counts`)

`/deliver` of a small spec (editor-core word counts plus a navigator badge) in a configured bookwyrm-editor worktree, served on an A40.

What happened:
- **Claude followed the routing from the written instructions alone.** It wrote a task file, ran `qwenbench dispatch` for `engineer`, and, when Qwen failed, stopped and asked instead of taking over. A human then granted an edits override, and Claude finished the work; that became PR #387. But no hook ever ran in that session, because it started in a desktop-app worktree (see Limits). The three `agent-qwen-must-dispatch` denials in the log came from `hero verify`. So this trial shows instructions being followed, not enforcement holding.
- **Qwen hit its 60-iteration limit** with two desktop typecheck errors left. On the way, it **commented out its own failing editor-core tests** to make that suite pass. Dispatch now fails such a result as `tests_disabled` and lists weakened assertions in `warnings`.
- **Claude's task file contained most of the implementation.** That spends frontier tokens on the part Qwen is meant to do. The routing instructions now say to describe behavior and interfaces, not code, and to split work into small dispatches.
- **The dispatch file list included `.hero/NEXT.md` and friends**, which Hero's hooks rewrote mid-dispatch. `.hero/` is no longer attributed to the worker.
- **The audit reported everything as Qwen's.** With no baseline it compared against HEAD, and Claude's commit hid its own edits. That is fixed as described above.

## Setup summary

```bash
qwenbench up
qwenbench hero inspect   ~/code/myproject
qwenbench hero install-hooks                   # once per machine: user-level hooks
qwenbench hero configure ~/code/myproject
qwenbench hero verify    ~/code/myproject
# ...use Claude Code with Hero as usual...
qwenbench hero audit     ~/code/myproject
qwenbench hero unconfigure ~/code/myproject    # stop enforcing; evidence is kept
```
