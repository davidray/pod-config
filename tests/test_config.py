import copy

import pytest
import yaml

from qwenbench.config import ModelsFile, RolePolicyFile, format_duration, parse_duration, resolve_profile
from qwenbench.paths import config_dir


def test_duration_parsing():
    assert parse_duration("30m") == 1800
    assert parse_duration("8h") == 28800
    assert parse_duration("90s") == 90
    assert parse_duration("off") is None
    assert parse_duration(0) is None
    with pytest.raises(ValueError):
        parse_duration("soon")
    assert format_duration(3725) == "1h02m05s"
    assert format_duration(None) == "off"


def test_profiles_resolve_identically_except_gpu(cfg):
    a, b = cfg.profile("a6000"), cfg.profile("l40s")
    assert a.gpu_type_id == "NVIDIA RTX A6000" and b.gpu_type_id == "NVIDIA L40S"
    # Comparability: same model, revision, runtime and generation on both GPUs.
    assert a.model == b.model
    assert a.runtime == b.runtime
    assert a.runtime_overrides == {} and b.runtime_overrides == {}
    assert a.runtime.max_model_len == 65536
    assert a.runtime.enable_prefix_caching
    assert 0.90 <= a.runtime.gpu_memory_utilization <= 0.92
    assert a.idle_timeout_s == 1800
    assert a.storage.volume_name != b.storage.volume_name  # no data center hosts both GPUs + volumes


def test_profile_overrides_merge(cfg):
    raw = copy.deepcopy(cfg.runpod.profiles["a6000"])
    raw.runtime_overrides = {"gpu_memory_utilization": 0.92}
    cfg.runpod.profiles["a6000"] = raw
    p = resolve_profile(cfg, "a6000")
    assert p.runtime.gpu_memory_utilization == 0.92
    assert p.runtime.max_model_len == 65536


def test_unknown_profile(cfg):
    with pytest.raises(KeyError):
        cfg.profile("h100")


def _models_doc():
    return yaml.safe_load((config_dir() / "models.yaml").read_text())


def test_model_revision_must_be_pinned():
    doc = _models_doc()
    doc["models"]["qwen3-coder-30b-fp8"]["revision"] = "main"
    with pytest.raises(ValueError, match="pinned"):
        ModelsFile(**doc)


def test_image_must_be_pinned():
    doc = _models_doc()
    doc["models"]["qwen3-coder-30b-fp8"]["image"] = "vllm/vllm-openai:latest"
    with pytest.raises(ValueError, match="pinned"):
        ModelsFile(**doc)


def test_role_policy_rejects_bad_targets():
    doc = yaml.safe_load((config_dir() / "role-policy.yaml").read_text())
    doc["agents"]["engineer"] = "gpt"
    with pytest.raises(ValueError, match="unknown target"):
        RolePolicyFile(**doc)


def test_pricing_is_config_not_code(cfg):
    assert cfg.pricing.gpu_rate("NVIDIA RTX A6000", "SECURE") > 0
    assert cfg.pricing.as_of
    with pytest.raises(KeyError):
        cfg.pricing.gpu_rate("NVIDIA H100", "SECURE")
