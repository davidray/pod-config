import pytest

from qwenbench.bench.costs import compute_costs


def test_observed_rate_preferred_over_list_price(cfg):
    c = compute_costs(cfg.pricing, gpu_type_id="NVIDIA RTX A6000", cloud="SECURE", gpu_count=1,
                      observed_rate=0.49, observed_rate_source="runpod-pod-cost", lifetime_s=3600,
                      storage_gb=60, storage_mode="network-volume")
    assert c.gpu_rate_per_hr == 0.49 and c.gpu_cost_usd == 0.49
    assert c.gpu_cost_source == "estimate (observed-rate)"


def test_list_price_fallback_is_labelled_with_date(cfg):
    c = compute_costs(cfg.pricing, gpu_type_id="NVIDIA L40S", cloud="SECURE", gpu_count=1,
                      observed_rate=1.09, observed_rate_source="list-price", lifetime_s=1800,
                      storage_gb=60, storage_mode="network-volume")
    assert c.gpu_cost_usd == pytest.approx(0.545)
    assert cfg.pricing.as_of in c.gpu_cost_source and "list-price" in c.gpu_cost_source


def test_storage_is_separate_and_prorated(cfg):
    c = compute_costs(cfg.pricing, gpu_type_id="NVIDIA RTX A6000", cloud="SECURE", gpu_count=1,
                      observed_rate=None, observed_rate_source=None, lifetime_s=730 * 3600,
                      storage_gb=60, storage_mode="network-volume")
    assert c.storage_monthly_usd == pytest.approx(4.20)  # 60 GB x $0.07
    assert c.storage_cost_usd == pytest.approx(4.20)      # a full month of lifetime
    assert c.total_usd == pytest.approx(c.gpu_cost_usd + c.storage_cost_usd)
    eph = compute_costs(cfg.pricing, gpu_type_id="NVIDIA RTX A6000", cloud="SECURE", gpu_count=1,
                        observed_rate=None, observed_rate_source=None, lifetime_s=3600, storage_gb=60,
                        storage_mode="ephemeral")
    assert eph.storage_cost_usd == 0


def test_authoritative_billing_supersedes_estimate(cfg):
    c = compute_costs(cfg.pricing, gpu_type_id="NVIDIA RTX A6000", cloud="SECURE", gpu_count=1,
                      observed_rate=0.49, observed_rate_source="runpod-pod-cost", lifetime_s=3600,
                      storage_gb=0, storage_mode="network-volume", billing_total=0.41)
    assert c.total_usd == 0.41 and c.gpu_cost_source.startswith("authoritative")
    assert c.gpu_cost_usd == 0.49  # the estimate is still kept for comparison


def test_community_cloud_and_multi_gpu(cfg):
    c = compute_costs(cfg.pricing, gpu_type_id="NVIDIA L40S", cloud="COMMUNITY", gpu_count=2,
                      observed_rate=None, observed_rate_source=None, lifetime_s=3600, storage_gb=0,
                      storage_mode="ephemeral")
    assert c.gpu_cost_usd == pytest.approx(0.79 * 2)
