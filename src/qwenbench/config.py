"""Typed loading of config/*.yaml. All tunables live in YAML, not code."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qwenbench.paths import config_dir

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d)?\s*$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def parse_duration(value: str | int | float | None) -> float | None:
    """'30m' -> 1800.0; 'off'/'none'/0 -> None (disabled)."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if value > 0 else None
    text = str(value).strip().lower()
    if text in {"off", "none", "disabled", "never", "0"}:
        return None
    m = _DURATION.match(text)
    if not m:
        raise ValueError(f"invalid duration {value!r}; use e.g. 90s, 30m, 8h, or 'off'")
    return float(m.group(1)) * _UNITS[m.group(2)]


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "off"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- models.yaml


class GenerationParams(Strict):
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int | None = None
    repetition_penalty: float | None = None
    max_tokens: int = 8192
    seed: int | None = None


class RuntimeConfig(Strict):
    max_model_len: int
    gpu_memory_utilization: float = Field(ge=0.5, le=0.98)
    enable_prefix_caching: bool = True
    tensor_parallel_size: int = 1
    kv_cache_dtype: str = "auto"
    max_num_seqs: int | None = None
    tool_call_parser: str | None = None
    enable_auto_tool_choice: bool = False
    enable_prompt_tokens_details: bool = True
    extra_args: list[str] = Field(default_factory=list)


class ModelSpec(Strict):
    hf_repo: str
    revision: str
    served_model_name: str
    quantization: str
    size_gb: float
    gated: bool = False
    image: str
    # Optional prebuilt image from containers/qwen-vllm/Dockerfile ("image
    # mode"). Unset = "bootstrap mode" on the pinned upstream `image`.
    launch_image: str | None = None
    vllm_version: str
    cuda_version: str
    runtime: RuntimeConfig
    generation: GenerationParams

    @field_validator("revision")
    @classmethod
    def _pinned(cls, v: str) -> str:
        if v in {"main", "master", "latest"}:
            raise ValueError("model revision must be a pinned commit SHA, not a branch")
        return v

    @field_validator("image")
    @classmethod
    def _image_pinned(cls, v: str) -> str:
        if v.endswith(":latest") or ":" not in v.rsplit("/", 1)[-1]:
            raise ValueError("container image must be pinned to an explicit tag or digest")
        return v


class ModelsFile(Strict):
    models: dict[str, ModelSpec]


# ---------------------------------------------------------------- runpod.yaml


class StorageConfig(Strict):
    mode: Literal["network-volume", "ephemeral"] = "network-volume"
    size_gb: int = 60
    mount_path: str = "/workspace"
    volume_name: str | None = None


class ProfileDefaults(Strict):
    model: str
    cloud: Literal["SECURE", "COMMUNITY"] = "SECURE"
    gpu_count: int = 1
    container_disk_gb: int = 30
    endpoint_mode: Literal["proxy", "tcp"] = "proxy"
    port: int = 8000
    watchdog_port: int = 8001
    idle_timeout: str = "30m"
    startup_timeout: str = "25m"
    max_session: str = "8h"
    max_spend_usd: float | None = None
    storage: StorageConfig = Field(default_factory=StorageConfig)


class ProfileSpec(BaseModel):
    """A profile as written in YAML: sparse overrides on top of `defaults`."""

    model_config = ConfigDict(extra="forbid")
    description: str = ""
    gpu_type_id: str
    data_center_ids: list[str] = Field(default_factory=list)
    storage: dict[str, Any] = Field(default_factory=dict)
    runtime_overrides: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None
    cloud: Literal["SECURE", "COMMUNITY"] | None = None
    gpu_count: int | None = None
    container_disk_gb: int | None = None
    endpoint_mode: Literal["proxy", "tcp"] | None = None
    idle_timeout: str | None = None
    startup_timeout: str | None = None
    max_session: str | None = None
    max_spend_usd: float | None = None


class ApiConfig(Strict):
    base_url: str = "https://api.runpod.io"
    request_timeout_s: float = 30
    pod_name_prefix: str = "qwenbench-"


class Guards(Strict):
    max_gpu_count: int = 1
    allow_concurrent_profiles: bool = False


class RunpodFile(Strict):
    api: ApiConfig = Field(default_factory=ApiConfig)
    defaults: ProfileDefaults
    guards: Guards = Field(default_factory=Guards)
    profiles: dict[str, ProfileSpec]
    # Order `qwenbench up` (no profile) tries, and the order a "qwen" route picks a ready endpoint.
    profile_preference: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _preference_known(self) -> RunpodFile:
        unknown = [n for n in self.profile_preference if n not in self.profiles]
        if unknown:
            raise ValueError(f"profile_preference names unknown profiles: {unknown}")
        return self


class Profile(Strict):
    """Fully resolved profile: defaults merged with per-profile overrides."""

    name: str
    description: str
    gpu_type_id: str
    data_center_ids: list[str]
    model_key: str
    model: ModelSpec
    cloud: Literal["SECURE", "COMMUNITY"]
    gpu_count: int
    container_disk_gb: int
    endpoint_mode: Literal["proxy", "tcp"]
    port: int
    watchdog_port: int
    idle_timeout_s: float | None
    startup_timeout_s: float
    max_session_s: float | None
    max_spend_usd: float | None
    storage: StorageConfig
    runtime: RuntimeConfig
    runtime_overrides: dict[str, Any]

    @property
    def pod_name(self) -> str:
        return f"qwenbench-{self.name}"


# ---------------------------------------------------------------- pricing.yaml


class PricingFile(Strict):
    as_of: str
    source: str
    currency: str = "USD"
    gpu_hourly: dict[str, dict[str, float]]
    storage_monthly_per_gb: dict[str, float]
    hours_per_month: float = 730

    def gpu_rate(self, gpu_type_id: str, cloud: str) -> float:
        try:
            return self.gpu_hourly[gpu_type_id][cloud]
        except KeyError as e:
            raise KeyError(f"no list price for {gpu_type_id!r}/{cloud} in config/pricing.yaml") from e


# ---------------------------------------------------------------- role-policy.yaml


class ProviderRule(Strict):
    type: Literal["openai-compatible", "claude"]
    model_pattern: str
    compute: str | None = None

    @field_validator("model_pattern")
    @classmethod
    def _regex(cls, v: str) -> str:
        re.compile(v)
        return v


class DispatchPolicy(Strict):
    max_request_retries: int = 3
    max_attempts_per_task: int = 2


class Enforcement(Strict):
    unknown_agents: Literal["deny", "frontier"] = "deny"
    frontier_writable: list[str] = Field(default_factory=list)
    protected: list[str] = Field(default_factory=list)
    dispatch: DispatchPolicy = Field(default_factory=DispatchPolicy)


HERO_ROLES = ("design", "execution", "review")


class RolePolicyFile(Strict):
    version: int
    providers: dict[str, ProviderRule]
    agents: dict[str, str]
    builtin_agents: dict[str, str] = Field(default_factory=dict)
    enforcement: Enforcement = Field(default_factory=Enforcement)

    @model_validator(mode="after")
    def _targets_valid(self) -> RolePolicyFile:
        valid = set(HERO_ROLES) | set(self.providers) | {"deny"}
        for table in (self.agents, self.builtin_agents):
            for agent, target in table.items():
                if target not in valid:
                    raise ValueError(f"agent {agent!r} maps to unknown target {target!r}; valid: {sorted(valid)}")
        return self


# ---------------------------------------------------------------- loading


def _load_yaml(path: Path) -> Any:
    with path.open() as fh:
        return yaml.safe_load(fh)


class Config(BaseModel):
    models: ModelsFile
    runpod: RunpodFile
    pricing: PricingFile
    policy: RolePolicyFile
    root: Path

    def profile(self, name: str) -> Profile:
        if name not in self.runpod.profiles:
            raise KeyError(f"unknown profile {name!r}; known: {', '.join(sorted(self.runpod.profiles))}")
        return resolve_profile(self, name)

    def profile_names(self) -> list[str]:
        return sorted(self.runpod.profiles)

    def preferred_profiles(self) -> list[str]:
        return self.runpod.profile_preference or self.profile_names()


def resolve_profile(cfg: Config, name: str, **overrides: Any) -> Profile:
    d = cfg.runpod.defaults
    p = cfg.runpod.profiles[name]

    def pick(field: str) -> Any:
        if field in overrides and overrides[field] is not None:
            return overrides[field]
        val = getattr(p, field, None)
        return val if val is not None else getattr(d, field)

    model_key = pick("model")
    if model_key not in cfg.models.models:
        raise KeyError(f"profile {name!r} references unknown model {model_key!r}")
    model = cfg.models.models[model_key]
    storage = StorageConfig(**{**d.storage.model_dump(), **p.storage})
    if storage.mode == "network-volume" and not storage.volume_name:
        raise ValueError(f"profile {name!r} uses a network volume but has no storage.volume_name")
    runtime = RuntimeConfig(**{**model.runtime.model_dump(), **p.runtime_overrides})
    startup = parse_duration(pick("startup_timeout"))
    if startup is None:
        raise ValueError("startup_timeout cannot be disabled")
    return Profile(
        name=name,
        description=p.description,
        gpu_type_id=p.gpu_type_id,
        data_center_ids=p.data_center_ids,
        model_key=model_key,
        model=model,
        cloud=pick("cloud"),
        gpu_count=pick("gpu_count"),
        container_disk_gb=pick("container_disk_gb"),
        endpoint_mode=pick("endpoint_mode"),
        port=d.port,
        watchdog_port=d.watchdog_port,
        idle_timeout_s=parse_duration(pick("idle_timeout")),
        startup_timeout_s=startup,
        max_session_s=parse_duration(pick("max_session")),
        max_spend_usd=overrides.get("max_spend_usd", p.max_spend_usd if p.max_spend_usd is not None else d.max_spend_usd),
        storage=storage,
        runtime=runtime,
        runtime_overrides=p.runtime_overrides,
    )


def load_config(root: Path | None = None) -> Config:
    base = root or config_dir()
    return Config(
        models=ModelsFile(**_load_yaml(base / "models.yaml")),
        runpod=RunpodFile(**_load_yaml(base / "runpod.yaml")),
        pricing=PricingFile(**_load_yaml(base / "pricing.yaml")),
        policy=RolePolicyFile(**_load_yaml(base / "role-policy.yaml")),
        root=base,
    )
