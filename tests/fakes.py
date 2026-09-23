"""Test doubles: a scripted OpenAI-compatible (vLLM-like) HTTP server and a fake Runpod API."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx


def tool_call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "arguments": args}


@dataclass
class Turn:
    """One scripted model response."""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    content: str = ""
    status: int = 200          # non-200 simulates an HTTP failure for this attempt
    model: str | None = None   # override the served model name (identity tests)
    prompt_tokens: int = 1200
    cached_tokens: int = 800


class FakeOpenAIServer:
    """Loopback server speaking the subset of the vLLM OpenAI API we use.

    Streams chat completions as SSE with content and tool-call deltas split
    across chunks (like vLLM's qwen3_coder parser), then a usage chunk.
    """

    def __init__(self, script: list[Turn] | Callable[[dict], Turn], model: str = "qwen3-coder-30b-a3b-fp8",
                 api_key: str = "test-endpoint-key-123456", root: str = "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"):
        self.script = script
        self.model = model
        self.api_key = api_key
        self.root = root
        self.requests: list[dict[str, Any]] = []
        self.calls = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _auth(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {server.api_key}"

            def _json(self, code: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.path == "/health":
                    return self._json(200, {})
                if self.path == "/v1/models":
                    if not self._auth():
                        return self._json(401, {"error": "unauthorized"})
                    return self._json(200, {"data": [{"id": server.model, "root": server.root,
                                                      "max_model_len": 65536, "object": "model"}]})
                return self._json(404, {})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self._auth():
                    return self._json(401, {"error": "unauthorized"})
                server.requests.append(body)
                turn = server.next_turn(body)
                if turn.status != 200:
                    return self._json(turn.status, {"error": "scripted failure"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                model = turn.model or server.model
                out_tokens = 0
                for chunk in server.chunks(turn):
                    out_tokens += 1
                    self.wfile.write(f"data: {json.dumps({'model': model, 'choices': [chunk]})}\n\n".encode())
                    self.wfile.flush()
                usage = {"prompt_tokens": turn.prompt_tokens, "completion_tokens": max(out_tokens, 1) * 7,
                         "total_tokens": turn.prompt_tokens + out_tokens * 7,
                         "prompt_tokens_details": {"cached_tokens": turn.cached_tokens}}
                self.wfile.write(f"data: {json.dumps({'model': model, 'choices': [], 'usage': usage})}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def next_turn(self, body: dict) -> Turn:
        self.calls += 1
        if callable(self.script):
            return self.script(body)
        if not self.script:
            return Turn(tool_calls=[tool_call("finish", status="failed", summary="script exhausted")])
        return self.script.pop(0)

    @staticmethod
    def chunks(turn: Turn):
        if turn.content:
            for i in range(0, len(turn.content), 5):
                yield {"index": 0, "delta": {"content": turn.content[i:i + 5]}}
        for idx, tc in enumerate(turn.tool_calls):
            args = json.dumps(tc["arguments"])
            yield {"index": 0, "delta": {"tool_calls": [{"index": idx, "id": f"call_{idx}", "type": "function",
                                                         "function": {"name": tc["name"], "arguments": ""}}]}}
            for i in range(0, len(args), 11):
                yield {"index": 0, "delta": {"tool_calls": [{"index": idx, "function": {"arguments": args[i:i + 11]}}]}}
        yield {"index": 0, "delta": {}, "finish_reason": "tool_calls" if turn.tool_calls else "stop"}

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> FakeOpenAIServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------------- Runpod


def pod_json(pod_id: str = "pod_abc", name: str = "qwenbench-a6000", status: str = "RUNNING",
             cost: float = 0.49, gpu: str = "NVIDIA RTX A6000", dc: str = "CA-MTL-3") -> dict[str, Any]:
    """Shape per https://api.runpod.io/v2/openapi.json `Pod` (fields we rely on)."""
    return {
        "id": pod_id, "name": name, "status": status, "actions": ["stop", "restart", "terminate"],
        "image": "vllm/vllm-openai:v0.30.0-cu129", "args": "", "disk": 30, "mounts": {}, "ports": ["8000/http"],
        "env": {}, "registry": None, "cloud": "SECURE", "dataCenterId": dc, "cudaVersion": "12.9",
        "ssh": {}, "template": None, "cost": cost if status in ("RUNNING", "STARTING", "PROVISIONING") else 0.0,
        "locked": False, "globalNetworking": {}, "gpu": {"id": gpu, "count": 1, "vcpuCount": 9, "memory": 50},
        "runtime": {"uptime": 120, "gpus": [], "ports": [{"private": 8000, "public": 40123, "type": "tcp",
                                                         "ip": "203.0.113.7"}]} if status == "RUNNING" else None,
        "createdAt": "2026-09-23T10:00:00Z", "startedAt": "2026-09-23T10:00:05Z",
    }


class FakeRunpod:
    """In-memory Runpod v2 API behind an httpx.MockTransport."""

    def __init__(self, capacity_errors: set[str] | None = None, statuses: list[str] | None = None):
        self.pods: dict[str, dict] = {}
        self.volumes: dict[str, dict] = {}
        self.created: list[dict] = []
        self.terminated: list[str] = []
        self.capacity_errors = capacity_errors or set()
        self.statuses = statuses or ["PROVISIONING", "STARTING", "RUNNING"]
        self.gpu_availability: dict[str, str] = {}
        self._n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/v2/pods" and method == "GET":
            return httpx.Response(200, json={"pods": list(self.pods.values()),
                                             "pagination": {"nextCursor": None, "hasNextPage": False}})
        if path == "/v2/pods" and method == "POST":
            body = json.loads(request.content)
            dc = (body.get("dataCenterIds") or ["?"])[0]
            if dc in self.capacity_errors:
                return httpx.Response(400, json={"title": "Bad Request", "status": 400,
                                                 "detail": f"no {body['gpu']['id']} available in {dc}"})
            self._n += 1
            pid = f"pod_{self._n}"
            self.created.append(body)
            pod = pod_json(pid, body["name"], self.statuses[0], gpu=body["gpu"]["id"], dc=dc)
            pod["_status_iter"] = list(self.statuses)
            self.pods[pid] = pod
            return httpx.Response(201, json=self._public(pod))
        if path.startswith("/v2/pods/") and path.endswith("/logs"):
            text = "".join(f"id: {i}\ndata: {json.dumps({'source': 'container', 'line': f'line {i}', 'ts': 'x'})}\n\n"
                           for i in range(3))
            return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})
        if path.startswith("/v2/pods/"):
            pid = path.split("/")[3]
            pod = self.pods.get(pid)
            if method == "DELETE" or (method == "POST" and json.loads(request.content).get("action") == "terminate"):
                if not pod:
                    return httpx.Response(404, json={"title": "Not Found", "status": 404, "detail": "no pod"})
                self.terminated.append(pid)
                del self.pods[pid]
                return httpx.Response(204)
            if not pod:
                return httpx.Response(404, json={"title": "Not Found", "status": 404, "detail": "no pod"})
            it = pod["_status_iter"]
            if len(it) > 1:
                it.pop(0)
            pod.update({k: v for k, v in pod_json(pid, pod["name"], it[0], gpu=pod["gpu"]["id"],
                                                    dc=pod["dataCenterId"]).items()})
            pod["_status_iter"] = it
            return httpx.Response(200, json=self._public(pod))
        if path == "/v2/network-volumes" and method == "GET":
            return httpx.Response(200, json={"networkVolumes": list(self.volumes.values())})
        if path == "/v2/network-volumes" and method == "POST":
            body = json.loads(request.content)
            vid = f"vol_{len(self.volumes) + 1}"
            self.volumes[vid] = {"id": vid, "name": body["name"], "size": body["size"],
                                 "dataCenter": body["dataCenter"], "type": "STANDARD"}
            return httpx.Response(201, json=self.volumes[vid])
        if path.startswith("/v2/catalog/gpus/"):
            gpu = path.rsplit("/", 1)[1].replace("%20", " ")
            return httpx.Response(200, json={"id": gpu, "availability": self.gpu_availability.get(gpu, "LOW"),
                                             "price": {"secure": 0.53, "community": 0.33}})
        if path == "/v2/catalog/datacenters":
            return httpx.Response(200, json={"dataCenters": [
                {"id": "CA-MTL-3", "gpuAvailability": [{"id": "NVIDIA RTX A6000", "availability": "LOW"}],
                 "networkVolumeTypes": ["STANDARD"]},
                {"id": "EU-RO-1", "gpuAvailability": [{"id": "NVIDIA RTX A6000", "availability": "HIGH"}],
                 "networkVolumeTypes": ["STANDARD"]},
            ]})
        if path == "/v2/billing/pods":
            return httpx.Response(200, json={"records": [{"podId": "pod_1", "totalAmount": 0.41, "gpuAmount": 0.4,
                                                          "cpuAmount": 0, "diskAmount": 0.01,
                                                          "startTime": "a", "endTime": "b"}],
                                             "metadata": {"totals": {"totalAmount": 0.41, "gpuAmount": 0.4,
                                                                     "cpuAmount": 0, "diskAmount": 0.01}}})
        return httpx.Response(404, json={"title": "Not Found", "status": 404, "detail": path})

    @staticmethod
    def _public(pod: dict) -> dict:
        return {k: v for k, v in pod.items() if not k.startswith("_")}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
