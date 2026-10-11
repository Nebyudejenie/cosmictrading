"""AI gateway: free-only routing, safety, quota, breaker, cache, schema-validated output.

AI is optional: when no eligible free route exists, the result is WAITING_FOR_AI and the
caller degrades. Billing signals disable a provider immediately (budget is exactly $0).
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from cosmic.ai import state
from cosmic.ai.adapters.http import Transport, make_client
from cosmic.ai.adapters.openai_compat import (
    AIBadResponseError,
    AIBillingSignalError,
    AIError,
    AIRateLimitedError,
    AIUnavailableError,
    OpenAICompatAdapter,
)
from cosmic.ai.policy import Route, eligible_routes
from cosmic.ai.registry import ProviderConfig, load_registry
from cosmic.ai.safety import (
    DataClass,
    OutboundBlockedError,
    UntrustedItem,
    build_messages,
    check_outbound,
)
from cosmic.audit.service import Actor, record
from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.jsonutil import canonical_hash
from cosmic.core.logs import get_logger
from cosmic.db.engine import Database
from cosmic.notify.outbox import notify_owners
from cosmic.ops.incidents import Category, Severity, raise_incident

log = get_logger(__name__)

TIERS: dict[str, tuple[str, ...]] = {
    "classify": ("SMALL",),
    "extract": ("SMALL",),
    "research_narrative": ("LARGE", "SMALL"),
    "red_team": ("LARGE", "SMALL"),
}
Status = Literal["OK", "CACHED", "WAITING_FOR_AI", "BLOCKED", "INVALID_OUTPUT"]


@dataclass
class AITask:
    task_type: str
    role_key: str
    instructions: str
    data: list[UntrustedItem]
    output_model: type[BaseModel]
    classification: DataClass
    max_tokens: int = 800
    cache_ttl_seconds: int = 3600
    forbidden_values: list[str] = field(default_factory=list)


@dataclass
class AIResult:
    status: Status
    output: BaseModel | None = None
    provider_key: str | None = None
    model_id: str | None = None
    reasons: list[str] = field(default_factory=list)


class AIGateway:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        *,
        transport: Transport | None = None,
        retry_base_seconds: float = 1.0,
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.providers: dict[str, ProviderConfig] = load_registry(settings)
        self._client = make_client(transport)
        self.retry_base = retry_base_seconds

    async def close(self) -> None:
        await self._client.aclose()

    def _adapter(self, p: ProviderConfig) -> OpenAICompatAdapter:
        key = p.api_key(self.settings)
        assert key is not None  # eligibility already required a key
        return OpenAICompatAdapter(
            provider_key=p.key, base_url=p.base_url, api_key=key, client=self._client
        )

    async def run(self, task: AITask) -> AIResult:
        if task.task_type not in TIERS:
            return AIResult("BLOCKED", reasons=[f"unknown task type {task.task_type}"])
        schema_hint = task.output_model.model_json_schema()
        messages = build_messages(task.instructions, schema_hint, task.data)
        try:
            check_outbound(messages, task.classification, task.forbidden_values)
        except OutboundBlockedError as exc:
            async with self.db.transaction() as conn:
                await raise_incident(
                    conn,
                    dedup_key=f"ai.outbound_blocked:{task.role_key}",
                    category=Category.SECURITY,
                    severity=Severity.WARNING,
                    title="AI request blocked by the outbound scanner",
                    now=self.clock.now(),
                    detail={"task": task.task_type, "reason": str(exc)},
                )
            return AIResult("BLOCKED", reasons=[str(exc)])
        request_hash = canonical_hash(
            {
                "t": task.task_type,
                "i": task.instructions,
                "s": schema_hint,
                "d": [(d.item_id, d.source, d.content) for d in task.data],
            }
        )
        now = self.clock.now()
        async with self.db.connect() as conn:
            cached = await state.cache_get(conn, request_hash, now)
            if cached is not None:
                try:
                    out = task.output_model.model_validate(cached.response)
                    return AIResult("CACHED", out, cached.provider_key, cached.model_id)
                except ValidationError:
                    pass
            routes, why = await eligible_routes(
                conn, self.providers, self.settings, now, TIERS[task.task_type]
            )
        if not routes:
            return AIResult("WAITING_FOR_AI", reasons=why or ["no eligible free AI route"])
        reasons: list[str] = []
        invalid_attempts = 0
        for route in routes:
            async with self.db.connect() as conn:
                ok, why_quota = await state.quota_available(conn, route.provider, self.clock.now())
            if not ok:
                reasons.append(f"{route.provider.key}: {why_quota}")
                continue
            result = await self._call(route, task, messages, request_hash, reasons)
            if result is None:
                continue
            if result == "INVALID":
                invalid_attempts += 1
                if invalid_attempts >= 2:
                    return AIResult("INVALID_OUTPUT", reasons=reasons)
                continue
            assert isinstance(result, BaseModel)
            async with self.db.transaction() as conn:
                await state.cache_put(
                    conn,
                    key=request_hash,
                    task_type=task.task_type,
                    response=result.model_dump(mode="json"),
                    provider_key=route.provider.key,
                    model_id=route.model_id,
                    now=self.clock.now(),
                    ttl_seconds=task.cache_ttl_seconds,
                )
            return AIResult("OK", result, route.provider.key, route.model_id, reasons)
        if invalid_attempts:
            return AIResult("INVALID_OUTPUT", reasons=reasons)
        return AIResult("WAITING_FOR_AI", reasons=reasons or ["all eligible routes failed"])

    async def _call(
        self,
        route: Route,
        task: AITask,
        messages: list[dict[str, str]],
        request_hash: str,
        reasons: list[str],
    ) -> BaseModel | Literal["INVALID"] | None:
        p, adapter = route.provider, self._adapter(route.provider)
        for attempt in range(2):
            now = self.clock.now()
            try:
                res = await adapter.chat(
                    model=route.model_id, messages=messages, max_tokens=task.max_tokens
                )
            except AIBillingSignalError as exc:
                await self._billing(p, route, task, request_hash, str(exc), exc.http_status)
                reasons.append(f"{p.key}: billing signal — provider disabled")
                return None
            except AIRateLimitedError as exc:
                async with self.db.transaction() as conn:
                    await state.log_usage(
                        conn,
                        provider_key=p.key,
                        model_id=route.model_id,
                        task_type=task.task_type,
                        role_key=task.role_key,
                        request_hash=request_hash,
                        status="RATE_LIMITED",
                        now=now,
                        http_status=429,
                        error="rate limited",
                    )
                    await state.record_rate_limited(conn, p.key, now, exc.retry_after)
                reasons.append(f"{p.key}: rate limited")
                return None
            except (AIUnavailableError, AIBadResponseError, AIError) as exc:
                async with self.db.transaction() as conn:
                    await state.log_usage(
                        conn,
                        provider_key=p.key,
                        model_id=route.model_id,
                        task_type=task.task_type,
                        role_key=task.role_key,
                        request_hash=request_hash,
                        status="TIMEOUT" if "timeout" in str(exc) else "ERROR",
                        now=now,
                        http_status=exc.http_status,
                        error=str(exc),
                    )
                    circuit = await state.record_failure(conn, p.key, now, str(exc))
                reasons.append(f"{p.key}: {exc}")
                if circuit == "OPEN" or not isinstance(exc, AIUnavailableError) or attempt == 1:
                    return None
                await asyncio.sleep(random.uniform(0, self.retry_base))  # noqa: S311 - jitter
                continue
            if res.reported_cost is not None and res.reported_cost > 0:
                await self._billing(
                    p, route, task, request_hash, f"provider reported cost {res.reported_cost}", 200
                )
                reasons.append(f"{p.key}: reported a non-zero cost — provider disabled")
                return None
            parsed = self._parse(task, res.text)
            async with self.db.transaction() as conn:
                await state.log_usage(
                    conn,
                    provider_key=p.key,
                    model_id=route.model_id,
                    task_type=task.task_type,
                    role_key=task.role_key,
                    request_hash=request_hash,
                    status="OK" if parsed is not None else "INVALID_OUTPUT",
                    now=now,
                    http_status=res.http_status,
                    prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens,
                    latency_ms=res.latency_ms,
                    reported_cost=res.reported_cost,
                )
                await state.record_success(conn, p.key, now)
            if parsed is None:
                reasons.append(f"{p.key}/{route.model_id}: output did not match the schema")
                return "INVALID"
            return parsed
        return None

    @staticmethod
    def _parse(task: AITask, text: str) -> BaseModel | None:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned[cleaned.find("{") :] if "{" in cleaned else cleaned
        try:
            return task.output_model.model_validate(json.loads(cleaned))
        except (ValueError, ValidationError):
            return None

    async def _billing(
        self,
        p: ProviderConfig,
        route: Route,
        task: AITask,
        request_hash: str,
        why: str,
        http_status: int | None,
    ) -> None:
        now = self.clock.now()
        async with self.db.transaction() as conn:
            await state.log_usage(
                conn,
                provider_key=p.key,
                model_id=route.model_id,
                task_type=task.task_type,
                role_key=task.role_key,
                request_hash=request_hash,
                status="BILLING_SIGNAL",
                now=now,
                http_status=http_status,
                error=why,
            )
            await state.disable(conn, p.key, now, f"billing signal: {why}")
            await raise_incident(
                conn,
                dedup_key=f"ai.billing_signal:{p.key}",
                category=Category.AI,
                severity=Severity.CRITICAL,
                title=f"{p.name} returned a billing signal; disabled",
                now=now,
                detail={"reason": why, "model": route.model_id},
            )
            await record(
                conn,
                action="ai.provider_disabled",
                actor=Actor.system("ai-gateway"),
                occurred_at=now,
                entity_type="ai_provider",
                entity_id=p.key,
                payload={"reason": why},
            )
            await notify_owners(
                conn,
                kind="ai_billing_signal",
                now=now,
                dedup_key=f"ai-billing:{p.key}:{now:%Y%m%d}",
                payload={
                    "provider": p.name,
                    "reason": why,
                    "action": "provider disabled; inference budget is $0",
                },
            )


def summarise(result: AIResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "provider": result.provider_key,
        "model": result.model_id,
        "reasons": result.reasons[:10],
    }
