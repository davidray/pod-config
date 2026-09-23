# ADR 0009: How costs are calculated

Status: accepted (2026-09-23)

## Decision
- **GPU cost:** rate × pod lifetime, where lifetime runs from request to run end or termination and includes startup.
- **Rate precedence:**
  1. the Runpod billing API (`/v2/billing/pods`), labeled **authoritative**, applied later by `qwen bench finalize` because billing can lag
  2. the pod's live `cost` field, labeled estimate (observed-rate)
  3. the list price from `config/pricing.yaml`, labeled estimate (list-price, as of DATE)
- **Storage** is reported separately: network volume GB × $/GB/month, prorated over the run, with the standing monthly charge listed alongside.
- **Cost per success** = total ÷ successful tasks. Failures and startup are included on purpose.
- **Prices live in config only.** No price appears in code.

## Consequences
Estimates can be recomputed at any time from `run.json`. The label always
travels with the number.
