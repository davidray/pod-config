"""Minimal Runpod REST v2 client (https://api.runpod.io/v2/openapi.json).

REST v1 (rest.runpod.io/v1) retires 2026-11-15 and GraphQL in early 2027, and
the `runpod` Python SDK still uses GraphQL, so we speak v2 directly with httpx.
Only the endpoints this project needs are wrapped. Responses are returned as
plain dicts; `models.py` parses the few fields we rely on.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import httpx

from qwenbench.runpod.models import NetworkVolume, Pod


class RunpodError(RuntimeError):
    def __init__(self, status: int, title: str, detail: str, errors: Any = None):
        msg = f"Runpod API {status} {title}: {detail}"
        if errors:
            msg += f" ({errors})"
        super().__init__(msg)
        self.status = status
        self.title = title
        self.detail = detail

    @property
    def is_capacity(self) -> bool:
        # 400 on create = rule violation / no capacity: try the next candidate.
        return self.status == 400

    @property
    def is_insufficient_balance(self) -> bool:
        return self.status == 402


def _raise_for(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    try:
        body = resp.json()
    except ValueError:
        body = {"title": resp.reason_phrase, "detail": resp.text[:500]}
    raise RunpodError(
        resp.status_code,
        str(body.get("title", resp.reason_phrase)),
        str(body.get("detail", body.get("message", ""))),
        body.get("errors"),
    )


class RunpodClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.runpod.io",
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
    ):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._max_retries = max_retries

    def close(self) -> None:
        self._http.close()

    # ---------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._http.request(method, path, **kw)
            except httpx.TransportError:
                if attempt > self._max_retries:
                    raise
                time.sleep(min(2**attempt, 10))
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt <= self._max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 10)
                time.sleep(delay)
                continue
            _raise_for(resp)
            return resp

    def _paginate(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict]:
        items: list[dict] = []
        params = dict(params or {})
        params.setdefault("limit", 100)
        while True:
            body = self._request("GET", path, params=params).json()
            items.extend(body.get(key) or [])
            page = body.get("pagination") or {}
            if not page.get("hasNextPage") or not page.get("nextCursor"):
                return items
            params["cursor"] = page["nextCursor"]

    # ---------------------------------------------------------------- pods

    def list_pods(self) -> list[Pod]:
        return [Pod.from_api(p) for p in self._paginate("/v2/pods", "pods")]

    def get_pod(self, pod_id: str) -> Pod | None:
        try:
            return Pod.from_api(self._request("GET", f"/v2/pods/{pod_id}").json())
        except RunpodError as e:
            if e.status == 404:
                return None
            raise

    def create_pod(self, body: dict[str, Any]) -> Pod:
        # No automatic retry on create beyond transport errors: a 400 means
        # "no capacity here" and the caller walks its candidate list.
        return Pod.from_api(self._request("POST", "/v2/pods", json=body).json())

    def pod_action(self, pod_id: str, action: str) -> Pod | None:
        resp = self._request("POST", f"/v2/pods/{pod_id}/action", json={"action": action})
        if resp.status_code == 204 or not resp.content:
            return None
        return Pod.from_api(resp.json())

    def terminate_pod(self, pod_id: str) -> bool:
        """Idempotent: returns False when the pod was already gone."""
        try:
            self._request("DELETE", f"/v2/pods/{pod_id}")
            return True
        except RunpodError as e:
            if e.status == 404:
                return False
            raise

    def stream_logs(
        self, pod_id: str, tail: int = 200, source: str | None = None, follow: bool = False
    ) -> Iterator[dict[str, Any]]:
        """GET /v2/pods/{id}/logs (Server-Sent Events). Yields {source, line, ts}."""
        params: dict[str, Any] = {"tail": tail}
        if source:
            params["source"] = source
        timeout = None if follow else httpx.Timeout(30, read=5)
        with self._http.stream("GET", f"/v2/pods/{pod_id}/logs", params=params,
                               headers={"Accept": "text/event-stream"}, timeout=timeout) as resp:
            if not resp.is_success:
                resp.read()
                _raise_for(resp)
            try:
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        try:
                            yield json.loads(payload)
                        except ValueError:
                            yield {"source": "unknown", "line": payload, "ts": None}
            except httpx.ReadTimeout:
                # Backfill finished and no new lines within the read window.
                if follow:
                    raise
                return

    # ---------------------------------------------------------------- volumes

    def list_network_volumes(self) -> list[NetworkVolume]:
        body = self._request("GET", "/v2/network-volumes").json()
        items = body.get("networkVolumes", body if isinstance(body, list) else [])
        return [NetworkVolume.from_api(v) for v in items]

    def create_network_volume(self, name: str, size_gb: int, data_center: str) -> NetworkVolume:
        body = {"name": name, "size": size_gb, "dataCenter": data_center}
        return NetworkVolume.from_api(self._request("POST", "/v2/network-volumes", json=body).json())

    def delete_network_volume(self, volume_id: str) -> None:
        self._request("DELETE", f"/v2/network-volumes/{volume_id}")

    # ---------------------------------------------------------------- catalog / billing

    def gpu_catalog(self, gpu_id: str | None = None, cloud: str | None = None, count: int = 1) -> list[dict]:
        params: dict[str, Any] = {"include": "AVAILABILITY", "product": "POD", "count": count}
        if cloud:
            params["cloud"] = cloud
        if gpu_id:
            body = self._request("GET", f"/v2/catalog/gpus/{quote(gpu_id, safe='')}", params=params).json()
            return [body]
        body = self._request("GET", "/v2/catalog/gpus", params=params).json()
        return body.get("gpus", body if isinstance(body, list) else [])

    def datacenters(self) -> list[dict]:
        body = self._request("GET", "/v2/catalog/datacenters", params={"include": "GPU_AVAILABILITY"}).json()
        return body.get("dataCenters", body if isinstance(body, list) else [])

    def pod_billing(self, pod_id: str, start: str, end: str | None = None, bucket: str = "hour") -> dict:
        # The API rejects startTime without endTime (400), so default the end to now.
        end = end or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        params: dict[str, Any] = {"podId": pod_id, "startTime": start, "endTime": end, "bucketSize": bucket}
        return self._request("GET", "/v2/billing/pods", params=params).json()
