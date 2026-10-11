"""OpenAI-compatible chat completions adapter (Groq, Gemini OpenAI endpoint, OpenRouter)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class AIError(Exception):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class AIRateLimitedError(AIError):
    def __init__(self, message: str, retry_after: float | None, http_status: int = 429) -> None:
        super().__init__(message, http_status=http_status)
        self.retry_after = retry_after


class AIBillingSignalError(AIError):
    """The provider indicated payment/credits: the route is not free. Disable immediately."""


class AIUnavailableError(AIError):
    pass


class AIBadResponseError(AIError):
    pass


@dataclass(frozen=True)
class ChatResult:
    text: str
    prompt_tokens: int | None
    completion_tokens: int | None
    reported_cost: Decimal | None
    latency_ms: int
    http_status: int


def _retry_after(headers: httpx.Headers) -> float | None:
    for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = headers.get(name)
        if raw:
            try:
                return float(raw.rstrip("s"))
            except ValueError:
                continue
    return None


_BILLING_WORDS = ("credit", "billing", "payment", "insufficient_quota", "upgrade your plan", "paid")


class OpenAICompatAdapter:
    def __init__(
        self, *, provider_key: str, base_url: str, api_key: str, client: httpx.AsyncClient
    ) -> None:
        self.provider_key = provider_key
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self.client = client

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float = 0.2,
        json_mode: bool = True,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.provider_key == "openrouter":
            # No provider fallback and no model fallback list: the exact free route or nothing.
            body["provider"] = {"allow_fallbacks": False}
            body["usage"] = {"include": True}
        started = time.monotonic()
        try:
            resp = await self.client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                timeout=TIMEOUT,
                headers={"Authorization": f"Bearer {self._key}"},
            )
        except httpx.TimeoutException as exc:
            raise AIUnavailableError("timeout") from exc
        except httpx.TransportError as exc:
            raise AIUnavailableError(f"connection error: {type(exc).__name__}") from exc
        latency = int((time.monotonic() - started) * 1000)
        text = resp.text[:2000]
        lowered = text.lower()
        if resp.status_code == 402 or (
            resp.status_code in (400, 403) and any(w in lowered for w in _BILLING_WORDS)
        ):
            raise AIBillingSignalError(
                f"billing signal (HTTP {resp.status_code})", http_status=resp.status_code
            )
        if resp.status_code == 429:
            raise AIRateLimitedError("rate limited", _retry_after(resp.headers))
        if resp.status_code >= 500:
            raise AIUnavailableError(
                f"provider error HTTP {resp.status_code}", http_status=resp.status_code
            )
        if resp.status_code != 200:
            raise AIError(f"request refused HTTP {resp.status_code}", http_status=resp.status_code)
        try:
            data = json.loads(resp.text, parse_float=Decimal)
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIBadResponseError("unexpected response structure", http_status=200) from exc
        usage = data.get("usage") or {}
        cost = usage.get("cost")
        return ChatResult(
            text=str(content or ""),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            reported_cost=None if cost is None else Decimal(str(cost)),
            latency_ms=latency,
            http_status=200,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        resp = await self.client.get(
            f"{self.base_url}/models",
            timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {self._key}"},
        )
        if resp.status_code != 200:
            raise AIError(f"model list HTTP {resp.status_code}", http_status=resp.status_code)
        data = json.loads(resp.text)
        models = data.get("data", [])
        return [m for m in models if isinstance(m, dict)]


async def openrouter_model_pricing(
    client: httpx.AsyncClient, base_url: str, model_id: str
) -> dict[str, str] | None:
    """Public pricing for one OpenRouter model (keyless endpoint)."""
    resp = await client.get(f"{base_url.rstrip('/')}/models", timeout=TIMEOUT)
    if resp.status_code != 200:
        raise AIError(f"pricing lookup HTTP {resp.status_code}", http_status=resp.status_code)
    for m in json.loads(resp.text).get("data", []):
        if m.get("id") == model_id:
            pricing = m.get("pricing") or {}
            return {str(k): str(v) for k, v in pricing.items() if v is not None}
    return None


def pricing_is_zero(pricing: dict[str, str]) -> bool:
    if not pricing:
        return False
    try:
        return all(Decimal(v) == 0 for v in pricing.values())
    except ArithmeticError:
        return False
