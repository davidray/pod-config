# ADR 0012: Use whichever GPU has capacity

Status: accepted (2026-09-24)

## Context
ADR 0002 made A6000 and L40S separate profiles, and Hero routing named one of
them (`qwen:a6000`). On 2026-09-24 the A6000 had no Secure Cloud capacity in
any data center for about 50 minutes, while an L40S came up at once. For
routine delegated work, it doesn't matter which GPU serves the model. The model
revision, image, vLLM argv and generation settings are identical on both, and
the full daveeval run passed 16/18 (L40S) and 17/18 (A6000).

## Decision
- `config/runpod.yaml` has `profile_preference: [a6000, l40s]`. The A6000 is first because it was about 40% cheaper per passed task.
- `qwenbench up` with no profile tries each in order and uses the first with capacity. A no-capacity attempt fails in seconds and creates nothing: the GPU catalog is checked before a volume is created, and a refused pod create leaves nothing behind. Any other failure stops the attempt instead of falling through. A profile that is already ready is reused.
- The Hero model id `qwen` (no profile) routes to whichever profile is ready, in the same order. `qwenbench hero configure` now writes `qwen` by default. `qwen:<profile>` still pins one GPU.
- If nothing is ready, dispatch still fails fast (exit 4) with no fallback to Claude.
- Benchmarks still name one profile. A comparison run must know which GPU it measured.

## Addendum (same day): A40 profile and ephemeral storage by default
The first live `qwenbench up` after this change went a6000 → l40s. It created
the L40S cache volume in the data center the catalog ranked best (US-IL-1),
then found no L40S there, and the volume now pinned every L40S start to that
data center. Minutes later neither the A6000 nor the L40S had stock in any data
center, while the A40 showed HIGH availability.

- **Storage.** The default is now `ephemeral`. A volume saves a few minutes of download but defeats the point of this ADR, which is to start wherever there is stock. `--storage network-volume` is still available. The empty L40S volume was deleted.
- **A40.** Added as `a40`: 48 GB, the same Ampere Marlin FP8 path as the A6000, $0.49/hr Secure. It is second in the preference order (`a6000, a40, l40s`) until a benchmark says otherwise. It has not been benchmarked yet.

## Consequences
The dispatch result and ledger record which profile actually served each
task, so audits still attribute work to a specific GPU.
