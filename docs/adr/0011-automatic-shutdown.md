# ADR 0011: Two-layer automatic shutdown

Status: accepted (2026-09-23)

## Decision
1. **In-pod supervisor (primary).** It survives laptop sleep and network loss.
   - **Idle signal:** vLLM Prometheus counters (`request_success_total`, `num_requests_running`, `num_requests_waiting`). The idle clock starts once the model serves.
   - **Limits:** idle, startup, max-session, max-spend and vLLM-crash.
   - **Action:** it terminates its own pod through the v2 API with the pod-scoped key, falling back to delete and then stop. It persists the reason on the volume and serves it on `/status`.
2. **Local guard (backstop).** Started detached by `qwenbench up`, using the account key. It terminates when:
   - the supervisor declared shutdown but the pod still bills after 5 minutes (catches an under-privileged pod key)
   - limits are exceeded by more than the grace period
   - the supervisor is unreachable for 15 minutes on a ready pod

Both are pure-decision functions with unit tests. The real supervisor script
is also run as a process in tests, idling out against a fake vLLM.

## Defaults
Idle 30 min, startup 25 min, session 8 h, $10 per session. Idle is disabled
only by an explicit `--idle-timeout off`, which prints a warning.

## Finding from the first real pods (2026-09-23)
Runpod's injected pod-scoped `RUNPOD_API_KEY` gets **HTTP 403** on
`GET /v2/pods/{own id}`, so the in-pod supervisor almost certainly cannot stop
its own pod with it. Without a further key, only the local guard can enforce
shutdown, and only while the workstation is awake.

Response:
- An optional `RUNPOD_SELF_STOP_API_KEY` (a separate, restricted Runpod key) is
  passed to the pod as `QWENBENCH_SELF_STOP_KEY`. The supervisor prefers it.
- `qwenbench up` warns loudly, and logs `self-stop-unavailable`, whenever the
  supervisor reports it cannot reach the API. `qwenbench doctor` warns when the
  key is not set.

Still to validate: which Runpod key permission level is the minimum that allows
stop/terminate. The first launch with a self-stop key should confirm
`runpod_api_auth_ok: true` in `qwenbench status`.
