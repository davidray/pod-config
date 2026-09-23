"""Instrumented OpenAI-compatible chat client.

Always streams (with `stream_options.include_usage`) so time-to-first-token is
measured on the client and so Runpod's Cloudflare proxy (~100s idle cap)
never sees a silent connection. Each HTTP attempt is recorded as a
RequestRecord; retries are counted, never hidden.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx


@dataclass
class RequestRecord:
    ts: str
    request_id: str
    model: str
    context: dict[str, Any]           # run_id, task_id, profile, trial, iteration, role...
    attempt: int                      # 1-based; attempt - 1 == retries before this one
    ok: bool
    http_status: int | None
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    ttft_s: float | None
    generation_s: float | None        # first token -> last token
    total_s: float
    output_tokens_per_s: float | None
    finish_reason: str | None
    tool_calls: int
    served_model: str | None          # `model` field echoed by the server

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str | None
    usage: dict[str, Any]
    served_model: str | None
    record: RequestRecord
    retries: int = 0

    def assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


class LLMError(RuntimeError):
    def __init__(self, message: str, records: list[RequestRecord]):
        super().__init__(message)
        self.records = records


RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


@dataclass
class ChatClient:
    base_url: str                     # ends in /v1
    api_key: str
    model: str
    sink: Callable[[RequestRecord], None] | None = None
    max_retries: int = 3
    connect_timeout_s: float = 20
    read_timeout_s: float = 95        # below Cloudflare's 100s cap between bytes
    transport: httpx.BaseTransport | None = None
    context: dict[str, Any] = field(default_factory=dict)
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        self._http = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(self.read_timeout_s, connect=self.connect_timeout_s),
            transport=self.transport,
        )

    def close(self) -> None:
        self._http.close()

    def list_models(self) -> list[dict[str, Any]]:
        resp = self._http.get("/models")
        resp.raise_for_status()
        return resp.json().get("data", [])

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        body.update({k: v for k, v in (params or {}).items() if v is not None})
        if tools:
            body["tools"] = tools
            body.setdefault("tool_choice", "auto")
        ctx = {**self.context, **(context or {})}
        records: list[RequestRecord] = []
        for attempt in range(1, self.max_retries + 2):
            result, record, retryable = self._attempt(body, ctx, attempt)
            records.append(record)
            if self.sink:
                self.sink(record)
            if result is not None:
                result.retries = attempt - 1
                return result
            if not retryable or attempt > self.max_retries:
                raise LLMError(f"chat request failed after {attempt} attempt(s): {record.error}", records)
            self.sleep(min(2 ** attempt, 20))
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ internals

    def _attempt(self, body: dict[str, Any], ctx: dict[str, Any], attempt: int):
        request_id = uuid.uuid4().hex[:16]
        t0 = time.monotonic()
        first_tok: float | None = None
        last_tok: float | None = None
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish: str | None = None
        served: str | None = None
        status: int | None = None

        def record(ok: bool, error: str | None) -> RequestRecord:
            total = time.monotonic() - t0
            out_tokens = usage.get("completion_tokens")
            gen = (last_tok - first_tok) if (first_tok is not None and last_tok is not None) else None
            tps = (out_tokens / gen) if (out_tokens and gen and gen > 0) else None
            details = usage.get("prompt_tokens_details") or {}
            return RequestRecord(
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                request_id=request_id,
                model=self.model,
                context=ctx,
                attempt=attempt,
                ok=ok,
                http_status=status,
                error=error,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=out_tokens,
                cached_input_tokens=details.get("cached_tokens"),
                ttft_s=round(first_tok - t0, 4) if first_tok is not None else None,
                generation_s=round(gen, 4) if gen is not None else None,
                total_s=round(total, 4),
                output_tokens_per_s=round(tps, 2) if tps else None,
                finish_reason=finish,
                tool_calls=len(calls),
                served_model=served,
            )

        try:
            with self._http.stream("POST", "/chat/completions", json=body) as resp:
                status = resp.status_code
                if resp.status_code != 200:
                    text = resp.read().decode("utf-8", "replace")[:500]
                    return None, record(False, f"HTTP {resp.status_code}: {text}"), resp.status_code in RETRYABLE_STATUS
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    served = chunk.get("model") or served
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        got = False
                        if delta.get("content"):
                            content.append(delta["content"])
                            got = True
                        for tc in delta.get("tool_calls") or []:
                            got = True
                            slot = calls.setdefault(tc.get("index", 0), {
                                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["function"]["arguments"] += fn["arguments"]
                        if got:
                            now = time.monotonic()
                            first_tok = first_tok if first_tok is not None else now
                            last_tok = now
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except (httpx.TransportError, httpx.StreamError) as e:
            return None, record(False, f"{type(e).__name__}: {e}"), True
        except json.JSONDecodeError as e:
            return None, record(False, f"malformed stream chunk: {e}"), True

        rec = record(True, None)
        result = ChatResult(
            content="".join(content),
            tool_calls=[calls[i] for i in sorted(calls)],
            finish_reason=finish,
            usage=usage,
            served_model=served,
            record=rec,
        )
        return result, rec, False


class JsonlSink:
    """Appends redacted records to a JSONL file."""

    def __init__(self, path, extra_secrets: list[str] | None = None):
        from qwenbench.secrets import redact

        self._redact = redact
        self.path = path
        self.extra = extra_secrets or []
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, record: RequestRecord) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(self._redact(record.to_dict(), self.extra)) + "\n")
