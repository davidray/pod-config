"""ModelProvider for any OpenAI-compatible server (vLLM here)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from qwenbench.metrics.llm import ChatClient, LLMError
from qwenbench.providers.base import Endpoint

PROBE_MESSAGES = [
    {"role": "system", "content": "You are a readiness probe. Reply with exactly: OK"},
    {"role": "user", "content": "Reply with OK."},
]


@dataclass
class OpenAICompatibleModel:
    ep: Endpoint
    transport: httpx.BaseTransport | None = None

    def endpoint(self) -> Endpoint:
        return self.ep

    def _root(self) -> str:
        return self.ep.base_url.rstrip("/").removesuffix("/v1")

    def health(self) -> dict[str, Any]:
        """Cheap checks: /health and /v1/models identity. No generation."""
        out: dict[str, Any] = {"health_ok": False, "model_listed": False, "models": [], "error": None}
        try:
            with httpx.Client(timeout=15, transport=self.transport) as http:
                h = http.get(f"{self._root()}/health")
                out["health_ok"] = h.status_code == 200
                m = http.get(f"{self.ep.base_url.rstrip('/')}/models",
                             headers={"Authorization": f"Bearer {self.ep.api_key}"})
                if m.status_code == 200:
                    data = m.json().get("data", [])
                    out["models"] = [
                        {"id": d.get("id"), "root": d.get("root"), "max_model_len": d.get("max_model_len")}
                        for d in data
                    ]
                    out["model_listed"] = any(d.get("id") == self.ep.model for d in data)
                else:
                    out["error"] = f"/v1/models HTTP {m.status_code}"
        except httpx.HTTPError as e:
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    def probe(self, sink=None) -> dict[str, Any]:
        """A real completion. Verifies auth, generation and served identity."""
        client = ChatClient(self.ep.base_url, self.ep.api_key, self.ep.model, sink=sink,
                            max_retries=0, transport=self.transport, context={"kind": "readiness-probe"})
        try:
            r = client.chat(PROBE_MESSAGES, params={"max_tokens": 8, "temperature": 0})
            ok = r.served_model == self.ep.model
            return {
                "ok": ok,
                "served_model": r.served_model,
                "reply": r.content.strip()[:40],
                "ttft_s": r.record.ttft_s,
                "total_s": r.record.total_s,
                "error": None if ok else f"server answered as {r.served_model!r}, expected {self.ep.model!r}",
            }
        except LLMError as e:
            return {"ok": False, "error": str(e)}
        finally:
            client.close()

    def metadata(self) -> dict[str, Any]:
        h = self.health()
        model = next((m for m in h["models"] if m["id"] == self.ep.model), None)
        return {
            "base_url": self.ep.base_url,
            "served_model": self.ep.model,
            "model_root": model and model.get("root"),
            "max_model_len": model and model.get("max_model_len"),
            **self.ep.metadata,
        }
