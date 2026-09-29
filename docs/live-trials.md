# Live trials and verdict

The benchmark ([benchmarking.md](benchmarking.md)) measures Qwen on fixed cases
with hidden validation. These trials measure the real workflow: a Claude Code
session running Hero's `/deliver` on a real repository (bookwyrm-editor), with
implementation routed to Qwen through `qwenbench dispatch`.

## Trial 1 (2026-09-24): `chapter-word-counts`, one large dispatch

Details are in [hero-routing.md](hero-routing.md#first-live-trial-2026-09-24-bookwyrm-editor-chapter-word-counts).

- **One dispatch for the whole spec** (editor-core counting plus a navigator UI change). Qwen hit its 60-iteration limit and commented out its own failing tests.
- **Claude finished the work** under a human-granted override. That became bookwyrm-editor PR #387.
- **No hook ever ran.** The session started in a fresh desktop-app worktree, so it was unenforced. Claude only followed the routing instructions. The user-level hooks ([ADR 0006](adr/0006-claude-code-enforcement.md)) were the fix.

## Trial 2 (2026-09-29): `weekly-writing-summary`, two small units

- **Setup:** a separate clone (`bookwyrm-editor-qwen`), the user-level hooks, and an A40 (the A6000 had no capacity).
- **The spec** was deliberately cut into two small dispatch units:
  1. core date logic in editor-core;
  2. a UI line in the Writing Progress card.
- **The result** landed as bookwyrm-editor PR #391. The delivery audit said SHIP on 11/11 acceptance criteria, and `hero spec verify` passed.

### Enforcement: held
- Every tool call ran through the hooks, in the app-created worktree.
- Claude stopped at the attempt limit and asked the human, instead of implementing it.
- There were no overrides.
- The audit attributes the implementation to Qwen dispatches.

The workflow still hit friction, all of it fixed in pod-config PR #7:
- Claude's commits were blocked, because the shell-write heuristic misread `<noreply@anthropic.com>` in the commit message.
- A stale session sent dispatch to a stopped pod.
- One dispatch died mid-run from the shell timeout.

### Qwen's work

| Dispatch | Unit | Iterations | Output tokens | Result |
|---|---|---|---|---|
| 1 | core | 43 | 17,621 | failed: test fixtures used wrong weekdays (a Saturday labelled Monday) and a wrong expected sum; also added its own date parser, which the spec forbids |
| 2 | core | 44 | 19,433 | failed: same tests still failing |
| 3 | core | 30 | 8,980 | failed: the "fix" read `"2026-05-20"` as UTC midnight, so Wednesday became Tuesday locally |
| 4 | core | 3 | n/a | died (killed by the shell timeout) |
| 5 | core | 18 | 7,471 | passed: Claude's task named the exact one-line fix |
| 6 | UI | 51 | 11,111 | failed: typecheck (`exactOptionalPropertyTypes`) |
| 7 | UI | 20 | 2,450 | passed |
| 8 | cleanup | 21 | 2,320 | "passed", but left the comments it was asked to remove |
| 9 | cleanup | 13 | 1,922 | passed |

- **Qwen in total:** about 71k output tokens and about 6M input tokens (mostly prefix-cached), about 25 minutes of agent time, on an A40 at $0.49/hr.
- **Claude in the same session:** 73 turns and about 37k output tokens. That covered planning, 9 task files, reviewing each diff, and working out and explaining each date bug. Trial 1's session used about 39k.

## Verdict

These points combine facts measured above with judgement. The judgement is marked as such.

1. **Qwen does the mechanical parts.** UI wiring, an optional prop, formatting, the cleanup: it got these right in one or two dispatches. That matches the benchmark, where bug fixes, refactors, small features and multi-file mechanical changes passed 3/3 on both GPUs.
2. **Qwen fails on correctness-sensitive logic and on tests.** Calendar arithmetic and the tests for it failed three times. It introduced a new timezone bug while fixing the tests, and it didn't converge even when told precisely what was wrong. That matches the benchmark's `aging-report-tests` (tests that check real logic) and trial 1, where it disabled failing tests. This is a property of the model, not of task size: trial 2's units were small and fully specified.
3. **The review cost doesn't go away.** Every Qwen error was caught, but by Claude: working out the calendar, the arithmetic and the timezone rules, then writing the corrective task. *Judgement:* for logic like this, catching and explaining the mistakes costs about as much frontier effort as writing the code. Claude's output tokens per trial (~37-39k) don't suggest a saving. There is no Claude-only baseline for these two specs, so this is an estimate, not a measurement.
4. **Enforcement works now,** after the user-level hooks and the trial fixes: in trial 2 nothing slipped past the hooks and no override was needed. Routing is now limited by what Qwen can do, not by the tooling.

## Recommendation

- **Don't route the whole execution role to Qwen.** Hero's roles are too coarse: `engineer` covers both "wire this prop" and "get the date maths right", and Qwen is only reliable at the first.
- **Keep test-writing on Claude,** both `test-architect` and `functional-qa-engineer`. Both trials and the benchmark agree.
- **Worth continuing on Qwen:** mechanical, easily verified roles such as the scrubbers (`comment-scrubber`, `deadcode-scrubber`, `legacy-scrubber`) and small refactors. Their output is quick to review, and a mistake is obvious in the diff.
- **What would change this:**
  - a Claude-only baseline for the same specs, to measure real savings;
  - a stronger open coding model on the same infrastructure. Everything here, from the benchmark and GPU profiles to dispatch, enforcement and audit, is model-agnostic.
- **For Hero:** the evidence argues for routing by task type (mechanical vs. logic), not only by agent. Hero's native model roles would need that granularity to make a cheaper executor worthwhile.
