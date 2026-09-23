# ADR 0010: Runpod REST v2, name-based discovery, bootstrap delivery

Status: accepted (2026-09-23). This changes the originally assumed architecture.

## Discoveries (from the live OpenAPI specs and docs, 2026-09-23)
- **REST v1 is retiring.** `rest.runpod.io/v1` retires on 2026-11-15, and GraphQL follows in early 2027. The `runpod` Python SDK (1.12) still uses GraphQL. v2 (`api.runpod.io/v2`) is labeled beta but is the supported future.
- **v2 capabilities:**
  - `POST /v2/pods/{id}/action` for start/stop/restart/terminate
  - an SSE logs endpoint, `GET /v2/pods/{id}/logs`
  - `/v2/billing/pods`
  - a catalog with per-data-center availability
- **Pod creation limits:** `gpu.id` takes a single type, and bodies over 100 KB are rejected.
- **Storage constraints:** a pod must be in its network volume's data center. **No data center offered network volumes for both A6000 and L40S.**
- **No native lifecycle limits:** there is no auto-stop, max-runtime or terminate-after field. Pods get a pod-scoped `RUNPOD_API_KEY` whose permissions are undocumented.
- **Proxy:** `https://{pod}-{port}.proxy.runpod.net` goes through Cloudflare, which enforces a roughly 100 s connection limit.

## Decisions
- **Client:** raw httpx against v2, not the SDK.
- **Discovery by name.** Pods are named `qwenbench-<profile>`, and volumes by `storage.volume_name`. There are no hardcoded resource IDs, and `down --all` only touches `qwenbench-*` pods.
- **One cache volume per profile**, holding the same pinned revision.
- **Bootstrap delivery:** the supervisor and entrypoint ship as a deterministic base64 tarball env var (about 8 KB) on the **unmodified pinned upstream image**. No registry or build step is needed, and the SHA-256 is recorded. An optional `Dockerfile` bakes the same files in (`launch_image`).
- **Streaming everywhere**, to live with the proxy limit. `endpoint_mode: tcp` is available if needed.
- **Idle shutdown:** an in-pod watchdog plus a local guard (ADR 0011).
