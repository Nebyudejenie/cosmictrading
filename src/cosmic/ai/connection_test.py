"""The owner's AI connection test (S81, the P0 demo directive's "a successful API connection
test alone is insufficient"). It runs in the worker, which holds the keys, and records each
step (append-only ``ai_provider_tests``):

1. **Authentication:** the key is accepted (no 401 or 403).
2. **Model availability:** the approved model is in the provider's model list.
3. **Actual inference:** the approved model analyses supplied XAUUSD multi-timeframe data
   through the same prompt and safety wrapping as the gold engine's second opinion.
4. **Structured response:** the answer validates against the second opinion's schema
   (direction, BUY/SELL/WAIT, confidence, why, conditions, risk, timing).

Timeouts, rate limits, refusals, a billing signal (never paid: it fails closed) and invalid
answers are each recorded with what happened. The test trades nothing; its answer is shown, never
acted on."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import desc, select

from cosmic.ai import keystore
from cosmic.ai.adapters.http import Transport, make_client
from cosmic.ai.adapters.openai_compat import (
    AIBillingSignalError,
    AIError,
    AIRateLimitedError,
    AIUnavailableError,
    OpenAICompatAdapter,
)
from cosmic.ai.gold_opinion import INSTRUCTIONS, GoldOpinion
from cosmic.ai.registry import load_registry
from cosmic.ai.safety import UntrustedItem, build_messages
from cosmic.audit.service import Actor
from cosmic.audit.service import record as audit
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.core.redaction import scrub_text
from cosmic.db.engine import Database
from cosmic.db.schema_ai import ai_model_approvals, ai_provider_tests

ACTOR = Actor.system("ai_connection_test")
MAX_TOKENS = 600
# Supplied XAUUSD data in the second opinion's own form (a fixed sample: the test is about the
# connection and the model's ability, not today's market).
SAMPLE = [
    UntrustedItem(
        "frames",
        "HFM's candles, read by the rules (sample)",
        "1m: candles 240, trend UP, close 4101.2, RSI 61.3, ATR 1.9 (0.046 %), support 4092.4, "
        "resistance 4104.8\n5m: candles 120, trend UP, close 4101.2, RSI 58.9, ATR 4.6 (0.112 %), "
        "support 4080.1, resistance 4106.3\n15m: candles 120, trend UP, close 4101.2, RSI 57.2, "
        "ATR 8.1 (0.198 %), support 4071.0, resistance 4110.5\n1h: candles 240, trend UP, close "
        "4101.2, RSI 55.8, ATR 14.7 (0.358 %), support 4052.6, resistance 4125.9\n1d: candles "
        "120, trend SIDEWAYS, close 4096.0, RSI 51.0, ATR 78.4 (1.914 %), support 3957.8, "
        "resistance 4199.3",
    ),
    UntrustedItem(
        "price",
        "HFM's price (sample)",
        "bid 4101.20, ask 4101.55, spread 0.35; the rules say BUY: H1 trend UP; D1 trend "
        "SIDEWAYS; M1 momentum turned up above its average",
    ),
    UntrustedItem(
        "session", "the market timing (sample)", "sessions open: London, New York; suitability GOOD"
    ),
]


async def _approved_model(db: Database, provider_key: str) -> str | None:
    async with db.connect() as conn:
        model: str | None = await conn.scalar(
            select(ai_model_approvals.c.model_id)
            .where(
                ai_model_approvals.c.provider_key == provider_key,
                ai_model_approvals.c.revoked_at.is_(None),
            )
            .order_by(desc(ai_model_approvals.c.approved_at))
            .limit(1)
        )
    return model


async def run(
    db: Database,
    settings: Settings,
    provider_key: str,
    now: datetime,
    *,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Test one provider end to end; the recorded result."""
    async with db.connect() as conn:
        await keystore.refresh(conn, settings)
    p = load_registry(settings).get(provider_key)
    result: dict[str, Any] = {
        "provider_key": provider_key,
        "model_id": None,
        "key_source": "NONE",
        "auth_ok": False,
        "model_ok": False,
        "inference_ok": False,
        "schema_ok": False,
        "latency_ms": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "detail": "",
        "answer": None,
    }
    steps: list[str] = []
    if p is None or not p.enabled:
        result["detail"] = "not a provider in the reviewed free-AI registry, or disabled"
        return await _record(db, result, now)
    key = p.api_key(settings)
    result["key_source"] = p.key_source(settings)
    if key is None:
        result["detail"] = "no API key: add one in AI Configuration"
        return await _record(db, result, now)
    model = await _approved_model(db, provider_key)
    result["model_id"] = model
    async with make_client(transport) as client:
        adapter = OpenAICompatAdapter(
            provider_key=p.key, base_url=p.base_url, api_key=key, client=client
        )
        try:
            listed = {str(m.get("id")) for m in await adapter.list_models()}
            result["model_ok"] = model is not None and model in listed
            steps.append(
                "model list read"
                + ("" if result["model_ok"] else f"; {model or 'no approved model'} not in it")
            )
        except AIError as exc:
            if exc.http_status in (401, 403):
                result["detail"] = f"authentication refused (HTTP {exc.http_status})"
                return await _record(db, result, now)
            steps.append(f"model list unavailable ({exc})")
        if model is None:
            result["detail"] = "; ".join([*steps, "no approved model: approve one first"])
            return await _record(db, result, now)
        messages = build_messages(INSTRUCTIONS, GoldOpinion.model_json_schema(), SAMPLE)
        try:
            chat = await adapter.chat(model=model, messages=messages, max_tokens=MAX_TOKENS)
        except AIBillingSignalError as exc:
            result["detail"] = "; ".join([*steps, f"a billing signal ({exc}): never paid, off"])
            return await _record(db, result, now)
        except AIRateLimitedError:
            result["auth_ok"] = True  # the provider recognised the key and limited it
            result["detail"] = "; ".join([*steps, "rate limited by the provider: try later"])
            return await _record(db, result, now)
        except AIUnavailableError as exc:
            result["detail"] = "; ".join([*steps, f"provider unavailable: {exc}"])
            return await _record(db, result, now)
        except AIError as exc:
            if exc.http_status in (401, 403):
                result["detail"] = "; ".join(
                    [*steps, f"authentication refused (HTTP {exc.http_status})"]
                )
            else:
                result["auth_ok"] = True
                result["detail"] = "; ".join([*steps, f"inference refused: {exc}"])
            return await _record(db, result, now)
        result.update(
            auth_ok=True,
            model_ok=True,  # it answered: the model is available
            inference_ok=True,
            latency_ms=chat.latency_ms,
            prompt_tokens=chat.prompt_tokens,
            completion_tokens=chat.completion_tokens,
        )
        try:
            answer = GoldOpinion.model_validate(json.loads(chat.text))
        except (ValueError, ValidationError) as exc:
            result["detail"] = "; ".join(
                [*steps, f"answered, but not in the required form: {str(exc)[:200]}"]
            )
            return await _record(db, result, now)
        result["schema_ok"] = True
        result["answer"] = answer.model_dump()
        result["detail"] = "; ".join(
            [*steps, f"{answer.recommendation} ({answer.confidence}): {answer.why}"]
        )
    return await _record(db, result, now)


async def _record(db: Database, result: dict[str, Any], now: datetime) -> dict[str, Any]:
    result["detail"] = scrub_text(result["detail"])[:1000]
    async with db.transaction() as conn:
        await conn.execute(ai_provider_tests.insert().values(id=uuid7(), at=now, **result))
        await audit(
            conn,
            action="ai.provider_tested",
            actor=ACTOR,
            occurred_at=now,
            entity_type="ai_provider",
            entity_id=result["provider_key"],
            payload={k: result[k] for k in ("auth_ok", "model_ok", "inference_ok", "schema_ok")},
        )
    return result
