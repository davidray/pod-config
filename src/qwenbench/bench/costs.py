"""Cost accounting for a benchmark run. See docs/COSTS.md.

Every figure carries a `source` label:
  authoritative        Runpod billing API records for the pod
  observed-rate        pod's live `cost` ($/hr) x observed lifetime
  list-price           config/pricing.yaml x observed lifetime
Storage is always an estimate (network volume $/GB/month prorated), reported
separately from GPU compute because it is billed whether or not a GPU runs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from qwenbench.config import PricingFile


@dataclass
class CostBreakdown:
    gpu_rate_per_hr: float | None
    gpu_rate_source: str
    gpu_lifetime_s: float | None          # pod requested -> run end (includes startup)
    gpu_cost_usd: float | None
    gpu_cost_source: str
    storage_gb: float
    storage_cost_usd: float
    storage_monthly_usd: float
    total_usd: float | None
    billing_api_usd: float | None = None   # when available, supersedes gpu_cost

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_costs(
    pricing: PricingFile,
    *,
    gpu_type_id: str,
    cloud: str,
    gpu_count: int,
    observed_rate: float | None,
    observed_rate_source: str | None,
    lifetime_s: float | None,
    storage_gb: float,
    storage_mode: str,
    billing_total: float | None = None,
) -> CostBreakdown:
    if observed_rate and observed_rate_source == "runpod-pod-cost":
        rate, rate_src = observed_rate, "observed-rate"
    else:
        try:
            rate, rate_src = pricing.gpu_rate(gpu_type_id, cloud) * gpu_count, f"list-price (as of {pricing.as_of})"
        except KeyError:
            rate, rate_src = observed_rate, "observed-rate" if observed_rate else "unknown"
    gpu_cost = round(rate * lifetime_s / 3600, 4) if (rate and lifetime_s) else None
    gpu_src = f"estimate ({rate_src})"
    if billing_total is not None:
        gpu_src = "authoritative (Runpod billing API)"
    per_gb = pricing.storage_monthly_per_gb.get("network_volume", 0.0) if storage_mode == "network-volume" else 0.0
    monthly = round(storage_gb * per_gb, 2)
    storage = round(monthly * ((lifetime_s or 0) / 3600) / pricing.hours_per_month, 4)
    compute = billing_total if billing_total is not None else gpu_cost
    total = round(compute + storage, 4) if compute is not None else None
    return CostBreakdown(rate, rate_src, lifetime_s, gpu_cost, gpu_src, storage_gb, storage, monthly, total,
                         billing_total)
