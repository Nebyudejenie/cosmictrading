"""Fake AI providers behind httpx.MockTransport (the real adapter code runs)."""

from __future__ import annotations

import json
from collections import deque
from typing import Any

import httpx


class FakeAI:
    def __init__(self) -> None:
        self.behaviours: dict[str, deque[Any]] = {"groq": deque(), "openrouter": deque()}
        self.default: dict[str, Any] = {
            "groq": {"thesis": "Trend intact.", "counterarguments": ["Range risk."]},
            "openrouter": {"thesis": "Router view.", "counterarguments": ["Noise."]},
        }
        self.pricing: dict[str, dict[str, str]] = {
            "vendor/small-model:free": {"prompt": "0", "completion": "0"}
        }
        self.calls: list[dict[str, Any]] = []

    def provider_of(self, request: httpx.Request) -> str:
        return "groq" if request.url.host == "api.groq.com" else "openrouter"

    def handler(self, request: httpx.Request) -> httpx.Response:
        prov = self.provider_of(request)
        if request.url.path.endswith("/models"):
            data = [{"id": k, "pricing": v} for k, v in self.pricing.items()]
            return httpx.Response(200, json={"data": data})
        body = json.loads(request.content)
        self.calls.append(
            {"provider": prov, "body": body, "auth": request.headers.get("authorization", "")}
        )
        b = self.behaviours[prov].popleft() if self.behaviours[prov] else ("ok", self.default[prov])
        kind, payload = b if isinstance(b, tuple) else ("ok", b)
        if kind == "status":
            return httpx.Response(
                payload[0],
                headers=payload[1] if len(payload) > 1 else None,
                text=payload[2] if len(payload) > 2 else "{}",
            )
        if kind == "raw":
            content = payload
            usage: dict[str, Any] = {"prompt_tokens": 10, "completion_tokens": 5}
        else:
            content = json.dumps(payload)
            usage = {"prompt_tokens": 120, "completion_tokens": 40}
        if kind == "cost":
            content, usage = (
                json.dumps(self.default[prov]),
                {"prompt_tokens": 1, "completion_tokens": 1, "cost": payload},
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}], "usage": usage}
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
