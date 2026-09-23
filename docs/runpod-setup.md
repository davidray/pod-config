# Runpod setup and operations

## Account and API key

1. Create a Runpod account and add credit. Pods stop when the balance hits $0.
2. Create an API key at *Settings → API Keys* with read/write access. It is used for:
   pods, network volumes, the GPU/data-center catalog, billing, and logs.
3. Put it in `.env` as `RUNPOD_API_KEY=...`. The file is git-ignored and `make setup` creates it with mode 600. You can instead export it in your shell; the environment wins over `.env`.

## Hugging Face

`Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` is **not gated** (verified 2026-09-23),
so no token is required. A read-only `HF_TOKEN` raises download rate limits;
when set, it is passed to the pod and nowhere else.

## What `qwenbench up` does

1. **Guards.** Refuses if another qwenbench pod is live (unless `--allow-concurrent`), or if the GPU count would exceed `guards.max_gpu_count`.
2. **Volume.** Finds the profile's network volume **by name** (`storage.volume_name`), or creates it in the profile's best-availability data center. No Runpod IDs live in this repo.
3. **Pod.** Creates it via `POST /v2/pods` with the pinned image, the bootstrap env, `gpu.minCudaVersion: 13.0` and the volume mounted at `/workspace`. A per-session random bearer token protects vLLM and the supervisor. The session is saved locally before waiting, so `qwenbench down` works even if you Ctrl-C.
4. **Readiness.** Polls pod status, the supervisor's `/status`, vLLM `/health` and `/v1/models`, then sends a real completion. It must come from the expected served model.
5. **Guard.** Starts the local guard process.

On any failure or Ctrl-C during startup the pod is terminated unless you pass
`--keep-on-failure`.

### Startup timing

- **First boot:** downloads about 31 GB. Expect 10-20 minutes depending on the data center, plus vLLM's torch.compile and CUDA-graph capture.
- **Later boots:** the entrypoint finds the pinned revision on the volume and sets `HF_HUB_OFFLINE=1`. The compile cache is persisted as well, so startup is dominated by loading weights from the network volume.

`qwenbench up` prints each phase and records the timings in the session and in
benchmark `run.json` (`session.startup`).

## Profiles

`config/runpod.yaml` defines `a6000` and `l40s`:

| | a6000 | l40s |
|---|---|---|
| GPU type id (verified in v2 catalog) | `NVIDIA RTX A6000` | `NVIDIA L40S` |
| Data centers with the GPU and network volumes (2026-09-23) | CA-MTL-3, EU-RO-1 | US-IL-1, EU-NL-1, US-TX-3 |
| Cache volume | `qwenbench-hf-cache-a6000` | `qwenbench-hf-cache-l40s` |

No data center offered network volumes for **both** GPUs at the time of
writing, so each profile has its own cache volume holding the same pinned
revision. `qwenbench doctor` re-checks availability live. Edit
`data_center_ids` when Runpod's inventory moves.

## Persistent storage

- Network volume: 60 GB by default, about $4.20/month each. See [COSTS.md](COSTS.md).
- A volume is bound to one data center, so a pod using it can only start there. If that data center is out of stock, `qwenbench up` says so. Options:
  - wait
  - `qwenbench up P --storage ephemeral`: any data center, re-downloads weights, no standing cost
  - `qwenbench storage delete P`, then edit `data_center_ids`
- Volumes never keep a GPU running. `qwenbench down` terminates pods, not volumes.

## Endpoint access

HTTPS through Runpod's proxy: `https://<pod>-8000.proxy.runpod.net/v1`. The
proxy sits behind Cloudflare, which cuts connections idle for about 100 s. All
qwenbench clients stream, so bytes flow continuously; the risk is a
time-to-first-token over 100 s on a huge prompt. `endpoint_mode: tcp`
(public IP + mapped port, no TLS) avoids the proxy if that ever bites.

`qwenbench endpoint a6000 --export --show-key` prints `OPENAI_BASE_URL` /
`OPENAI_API_KEY` for other OpenAI-compatible tools.

## Logs

`qwenbench logs P [-f]` uses the v2 logs API (Server-Sent Events). If that is
unavailable it falls back to the supervisor's `/logs`. vLLM logs are also
persisted to `/workspace/qwenbench/logs/` on the volume.

## Automatic shutdown

There are two independent layers.

1. **In-pod supervisor (primary).** It keeps working when your laptop sleeps.
   - **Idle:** no completed or running request, according to vLLM's Prometheus counters, for `idle_timeout`.
   - **Also:** startup timeout, max session, max spend, and a vLLM crash (with a 5-minute grace period to fetch logs).
   - **How it stops:** it calls `POST /v2/pods/$RUNPOD_POD_ID/action {"action":"terminate"}` with Runpod's pod-scoped `RUNPOD_API_KEY`, falling back to delete and then stop.
   - **Record:** the reason goes to `/workspace/qwenbench/shutdowns.jsonl` and the container log.
2. **Local guard (backstop).** Started by `qwenbench up`.
   - Uses your account key.
   - Terminates the pod if the supervisor declared a shutdown but the pod is still billing 5 minutes later. That would mean the pod-scoped key lacked permission, which Runpod does not document.
   - Also acts past idle/session/spend limits plus a grace period, or when the supervisor has been unreachable for 15 minutes.
   - Logs go to `~/.local/state/qwenbench/events.jsonl`. `qwenbench status` shows recent automatic shutdowns.

Override per session: `qwenbench up a6000 --idle-timeout 60m`. Disable
explicitly with `--idle-timeout off`, which prints a warning; max session and
max spend still apply unless you also turn those off.

## Cleanup

```bash
qwenbench down --all                 # every pod named qwenbench-*, idempotent
qwenbench status                     # confirm nothing is live
qwenbench storage list               # standing monthly storage charges
qwenbench storage delete a6000 --yes # stop paying for a cache volume
```
