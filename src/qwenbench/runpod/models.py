"""Parsed views of Runpod v2 API objects. Only fields we rely on are typed;
the raw dict is kept for debugging and forward compatibility."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

LIVE_STATUSES = {"PROVISIONING", "STARTING", "RUNNING"}
BILLABLE_STATUSES = LIVE_STATUSES  # EXITED pods bill disk only; see docs/COSTS.md


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class PortMapping:
    private: int
    public: int | None
    type: str
    ip: str | None


@dataclass
class Pod:
    id: str
    name: str
    status: str
    image: str | None
    cost_per_hr: float
    gpu_type_id: str | None
    gpu_count: int
    data_center_id: str | None
    cuda_version: str | None
    created_at: datetime | None
    started_at: datetime | None
    uptime_s: int | None
    ports: list[PortMapping]
    env: dict[str, str]
    actions: list[str]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Pod:
        gpu = d.get("gpu") or {}
        runtime = d.get("runtime") or {}
        ports = [
            PortMapping(
                private=int(p.get("private", 0)),
                public=p.get("public"),
                type=str(p.get("type", "")),
                ip=p.get("ip"),
            )
            for p in (runtime.get("ports") or [])
        ]
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            status=d.get("status", "UNKNOWN"),
            image=d.get("image"),
            cost_per_hr=float(d.get("cost") or 0.0),
            gpu_type_id=gpu.get("id"),
            gpu_count=int(gpu.get("count") or 0),
            data_center_id=d.get("dataCenterId"),
            cuda_version=d.get("cudaVersion"),
            created_at=parse_ts(d.get("createdAt")),
            started_at=parse_ts(d.get("startedAt")),
            uptime_s=runtime.get("uptime"),
            ports=ports,
            env=dict(d.get("env") or {}),
            actions=list(d.get("actions") or []),
            raw=d,
        )

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    def proxy_url(self, port: int) -> str:
        return f"https://{self.id}-{port}.proxy.runpod.net"

    def tcp_url(self, port: int) -> str | None:
        for p in self.ports:
            if p.private == port and p.public and p.ip:
                return f"http://{p.ip}:{p.public}"
        return None


@dataclass
class NetworkVolume:
    id: str
    name: str
    size_gb: int
    data_center: str
    type: str | None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> NetworkVolume:
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            size_gb=int(d.get("size") or 0),
            data_center=d.get("dataCenter") or d.get("dataCenterId") or "",
            type=d.get("type"),
        )
