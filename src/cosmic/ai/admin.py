"""Owner actions for AI routes (ADMINISTER_SYSTEM + fresh MFA) and the provider health view."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.ai import state
from cosmic.ai.adapters.http import AIHttpClient
from cosmic.ai.adapters.openai_compat import AIError, openrouter_model_pricing, pricing_is_zero
from cosmic.ai.policy import (
    active_attestation,
    model_eligibility,
    provider_eligibility,
    provider_state,
)
from cosmic.ai.registry import ProviderConfig, load_registry
from cosmic.audit.service import record
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.db.schema_ai import ai_model_approvals, ai_provider_attestations, ai_usage
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require

MAX_ATTESTATION_DAYS = 30
MAX_REVIEW_DAYS = 30


def _provider(settings: Settings, key: str) -> ProviderConfig:
    p = load_registry(settings).get(key)
    if p is None:
        raise ValidationFailedError("unknown provider")
    if not p.enabled:
        raise ConflictError(p.disabled_reason or "provider disabled in the registry")
    return p


def _url(value: str) -> str:
    value = value.strip()
    if not value.startswith("https://") or len(value) > 500:
        raise ValidationFailedError("evidence must be an https URL to the page you checked")
    return value


async def attest(
    conn: AsyncConnection,
    *,
    principal: Principal,
    settings: Settings,
    provider_key: str,
    statement: str,
    evidence_url: str,
    days: int,
    rpm_limit: int | None,
    rpd_limit: int | None,
    now: datetime,
) -> uuid.UUID:
    require(principal, Capability.ADMINISTER_SYSTEM, now=now, settings=settings)
    p = _provider(settings, provider_key)
    if len(statement.strip()) < 20:
        raise ValidationFailedError("describe what you verified (at least 20 characters)")
    if not 1 <= days <= MAX_ATTESTATION_DAYS:
        raise ValidationFailedError(f"attestations expire within {MAX_ATTESTATION_DAYS} days")
    att_id = uuid7()
    await conn.execute(
        ai_provider_attestations.insert().values(
            id=att_id,
            provider_key=p.key,
            statement=statement.strip()[:1000],
            evidence_url=_url(evidence_url),
            rpm_limit=rpm_limit,
            rpd_limit=rpd_limit,
            attested_by=principal.user_id,
            attested_at=now,
            expires_at=now + timedelta(days=days),
        )
    )
    await state.clear_disable(conn, p.key, now)
    await record(
        conn,
        action="ai.attested",
        actor=principal.actor,
        occurred_at=now,
        entity_type="ai_provider",
        entity_id=p.key,
        payload={"days": days, "evidence_url": evidence_url},
    )
    return att_id


async def approve_model(
    conn: AsyncConnection,
    *,
    principal: Principal,
    settings: Settings,
    provider_key: str,
    model_id: str,
    tier: str,
    evidence_url: str,
    context_limit: int | None,
    review_days: int,
    now: datetime,
) -> uuid.UUID:
    require(principal, Capability.ADMINISTER_SYSTEM, now=now, settings=settings)
    p = _provider(settings, provider_key)
    model_id = model_id.strip()
    if not p.model_allowed_by_pattern(model_id):
        raise ValidationFailedError(f"model ID not allowed for {p.name} (free routes only)")
    if tier not in ("SMALL", "LARGE"):
        raise ValidationFailedError("tier must be SMALL or LARGE")
    if not 1 <= review_days <= MAX_REVIEW_DAYS:
        raise ValidationFailedError(
            f"model approvals must be re-reviewed within {MAX_REVIEW_DAYS} days"
        )
    await conn.execute(
        update(ai_model_approvals)
        .where(
            ai_model_approvals.c.provider_key == p.key,
            ai_model_approvals.c.model_id == model_id,
            ai_model_approvals.c.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    approval_id = uuid7()
    await conn.execute(
        ai_model_approvals.insert().values(
            id=approval_id,
            provider_key=p.key,
            model_id=model_id,
            tier=tier,
            context_limit=context_limit,
            evidence_url=_url(evidence_url),
            approved_by=principal.user_id,
            approved_at=now,
            review_expires_at=now + timedelta(days=review_days),
        )
    )
    await record(
        conn,
        action="ai.model_approved",
        actor=principal.actor,
        occurred_at=now,
        entity_type="ai_provider",
        entity_id=p.key,
        payload={"model": model_id, "tier": tier, "review_days": review_days},
    )
    return approval_id


async def revoke_model(
    conn: AsyncConnection,
    *,
    principal: Principal,
    settings: Settings,
    approval_id: uuid.UUID,
    now: datetime,
) -> None:
    if not (principal.has(Capability.ADMINISTER_SYSTEM)):
        raise ConflictError("missing capability ADMINISTER_SYSTEM")
    await conn.execute(
        update(ai_model_approvals)
        .where(ai_model_approvals.c.id == approval_id)
        .values(revoked_at=now)
    )
    await record(
        conn,
        action="ai.model_revoked",
        actor=principal.actor,
        occurred_at=now,
        entity_type="ai_model",
        entity_id=approval_id,
    )


async def run_price_checks(
    conn: AsyncConnection, *, settings: Settings, client: AIHttpClient, now: datetime
) -> dict[str, str]:
    """Refresh live pricing for runtime-checked providers (OpenRouter).

    Anything other than exactly 0 for every price field blocks the model.
    """
    results: dict[str, str] = {}
    registry = load_registry(settings)
    rows = list(
        await conn.execute(
            select(ai_model_approvals).where(ai_model_approvals.c.revoked_at.is_(None))
        )
    )
    for a in rows:
        p = registry.get(a.provider_key)
        if p is None or not p.runtime_price_check:
            continue
        try:
            pricing = await openrouter_model_pricing(client, p.base_url, a.model_id)
            result = (
                "NOT_LISTED"
                if pricing is None
                else ("ZERO" if pricing_is_zero(pricing) else "NON_ZERO")
            )
        except AIError as exc:
            result = f"CHECK_FAILED: {exc}"[:100]
        await conn.execute(
            update(ai_model_approvals)
            .where(ai_model_approvals.c.id == a.id)
            .values(price_checked_at=now, price_check_result=result)
        )
        results[f"{a.provider_key}/{a.model_id}"] = result
    return results


async def provider_overview(
    conn: AsyncConnection,
    settings: Settings,
    now: datetime,
    key_status: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Provider health for the portal. ``key_status`` is what the worker reported
    (CONFIGURED / NOT_CONFIGURED / UNKNOWN); the web process holds no AI keys itself."""
    out: list[dict[str, Any]] = []
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for p in load_registry(settings).values():
        reported = (key_status or {}).get(p.key)
        present = p.api_key(settings) is not None or reported == "CONFIGURED"
        elig = await provider_eligibility(conn, p, settings, now, key_present=present)
        st = await provider_state(conn, p.key)
        att = await active_attestation(conn, p.key, now)
        models = list(
            await conn.execute(
                select(ai_model_approvals)
                .where(
                    ai_model_approvals.c.provider_key == p.key,
                    ai_model_approvals.c.revoked_at.is_(None),
                )
                .order_by(desc(ai_model_approvals.c.approved_at))
            )
        )
        usage = list(
            await conn.execute(
                select(
                    ai_usage.c.status,
                    func.count().label("n"),
                    func.coalesce(func.sum(ai_usage.c.reported_cost), 0).label("cost"),
                )
                .where(ai_usage.c.provider_key == p.key, ai_usage.c.created_at >= day_start)
                .group_by(ai_usage.c.status)
            )
        )
        if not p.enabled:
            status = "DISABLED"
        elif not present and reported == "UNKNOWN":
            status = "UNKNOWN"  # the worker (which holds the keys) has not reported recently
        elif not present:
            status = "NOT_CONFIGURED"
        elif elig.ok:
            status = "AVAILABLE"
        else:
            status = "UNAVAILABLE"
        out.append(
            {
                "provider": p,
                "status": status,
                "reasons": elig.reasons,
                "attestation": att,
                "state": st,
                "models": [{"row": m, "eligibility": model_eligibility(p, m, now)} for m in models],
                "usage": {u.status: u.n for u in usage},
                "reported_cost_today": sum((u.cost for u in usage), 0),
                "key_configured": present,
            }
        )
    return out
