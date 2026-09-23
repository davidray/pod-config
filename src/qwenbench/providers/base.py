"""Provider interfaces.

ComputeProvider owns *where the GPU runs* (Runpod Pods today; Runpod
Serverless, a workstation, DGX Spark etc. later). ModelProvider owns *how to
talk to the model* once it is up. Benchmarks, the dispatcher and Hero routing
only ever see an `Endpoint`, never a Runpod URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from qwenbench.config import Profile


@dataclass
class Endpoint:
    profile: str
    base_url: str               # OpenAI-compatible, ends with /v1
    api_key: str
    model: str                  # served model name to put in requests
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComputeStatus:
    profile: str
    exists: bool
    state: str                  # provider-native status, e.g. RUNNING
    live: bool                  # billing compute right now
    cost_per_hr: float | None
    uptime_s: float | None
    estimated_spend_usd: float | None
    detail: dict[str, Any] = field(default_factory=dict)


class ComputeProvider(Protocol):
    name: str

    def up(self, profile: Profile, **options: Any) -> Endpoint: ...
    def down(self, profile: Profile) -> bool: ...
    def down_all(self) -> list[str]: ...
    def status(self, profile: Profile) -> ComputeStatus: ...
    def logs(self, profile: Profile, tail: int = 200, follow: bool = False) -> Any: ...
    def endpoint(self, profile: Profile) -> Endpoint | None: ...


class ModelProvider(Protocol):
    def endpoint(self) -> Endpoint: ...
    def health(self) -> dict[str, Any]: ...
    def metadata(self) -> dict[str, Any]: ...
