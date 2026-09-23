# Costs

There are two separate meters. Every figure qwenbench prints says which one it
is and where the number came from.

## 1. GPU compute (only while a pod exists)

| Source | Used when | Label |
|---|---|---|
| Runpod billing API `GET /v2/billing/pods?podId=` | `qwen bench finalize RUN` after billing settles (lag is undocumented; try an hour later) | **authoritative** |
| Pod's live `cost` field (USD/hr) × observed lifetime | Normal case: read at startup and by the supervisor | estimate (observed-rate) |
| `config/pricing.yaml` list price × lifetime | Only if the live rate is unavailable | estimate (list-price, as of DATE) |

**Lifetime** runs from pod request to benchmark end, or to termination with
`--down-after`. It includes startup, because you pay for that too. Each task
also reports `gpu_attributable_s`, its wall time on the dedicated GPU, and
`estimated_compute_cost_usd`.

List prices on 2026-09-23 (update `config/pricing.yaml`, not code):

| GPU | Secure | Community |
|---|---|---|
| RTX A6000 48GB | $0.53/hr | $0.33/hr |
| L40S 48GB | $1.09/hr | $0.79/hr |

## 2. Storage (always billed, GPU or not)

| Storage | Price | qwenbench default |
|---|---|---|
| Network volume | $0.07/GB/month (<1 TB) | 60 GB per profile ≈ **$4.20/month each, $8.40 for both** |
| Container disk | $0.10/GB/month, running pods only | 30 GB, only while up |

Reports show storage **prorated over the run's lifetime** ("storage estimate").
The monthly standing charge is listed in `qwen storage list` and
`costs.storage_monthly_usd`. Delete volumes you are not using:
`qwen storage delete P`.

## Cost per successful task

`total estimated cost ÷ successful tasks` over the run: GPU lifetime (including
startup and failed tasks) plus prorated storage. It is intentionally
pessimistic: failures and startup are real costs of using the system.

## Guards

- Per-session `max_spend_usd`, default $10: the supervisor terminates at the cap, and the guard acts at the cap plus grace.
- `max_session`, default 8 h.
- At most one GPU at a time.
- Runpod's own account spend limit, $80/hr by default, is a further backstop outside qwenbench.
