"""Versioned risk policy (V6 §1806, §1815). Immutable versions; changes are new rows.

Every limit has documented semantics (TRD-31): measurement source, currency,
timezone/session boundary, reset, fee and unrealised-P&L treatment, and the action
when the data needed to evaluate it is unavailable (always: block new exposure).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import canonical_hash, to_jsonable
from cosmic.db.schema_market import assets, instruments, venues
from cosmic.db.schema_trading import risk_policies
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require


class BlackoutWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    start: datetime
    end: datetime
    reason: str = Field(min_length=3, max_length=200)

    @model_validator(mode="after")
    def _order(self) -> BlackoutWindow:
        if self.end <= self.start:
            raise ValueError("blackout end must be after start")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("blackout times must be timezone-aware")
        return self


class EmergencyPolicy(BaseModel):
    """What the kill switch does to outstanding orders and positions (V6 §1817)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cancel_unfilled_entries: bool = True
    cancel_protective_orders: Literal[False] = False
    liquidate_positions: Literal[False] = False


class VenueFee(BaseModel):
    """The owner's fee assumption for one venue, in bps of notional (B-08)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    venue: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,30}$")
    bps: Decimal = Field(ge=0, le=500)


Pct = Decimal


class RiskPolicyParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_instruments: tuple[str, ...]
    allowed_venues: tuple[str, ...]
    allowed_quote_currencies: tuple[str, ...]
    allowed_order_types: tuple[Literal["LIMIT"], ...] = ("LIMIT",)
    allowed_time_in_force: tuple[Literal["GTC", "IOC", "GTD"], ...] = ("GTC", "GTD", "IOC")
    allow_short: Literal[False] = False
    max_leverage: Decimal = Decimal(1)

    max_order_notional: Decimal = Field(gt=0)
    max_position_notional: Decimal = Field(gt=0)
    max_gross_exposure: Decimal = Field(gt=0)
    max_concentration_pct: Pct = Field(gt=0, le=100)
    max_daily_loss: Decimal = Field(gt=0)
    max_drawdown_pct: Pct = Field(gt=0, le=100)
    max_loss_streak: int = Field(ge=1, le=100)
    max_open_orders: int = Field(ge=1, le=100)

    max_price_age_seconds: int = Field(ge=5, le=3600)
    allow_delayed_data: bool
    max_spread_bps: Decimal = Field(gt=0)
    require_cross_source_check: bool = True
    max_cross_source_deviation_bps: Decimal = Field(gt=0)
    require_venue_status: bool = True
    max_slippage_bps: Decimal = Field(ge=0)
    max_cost_bps: Decimal = Field(gt=0)
    rounding_tolerance_bps: Decimal = Field(ge=0)
    fee_bps_assumption: Decimal = Field(ge=0)
    # Per-venue fee assumptions (B-08), e.g. Binance's confirmed schedule; venues not listed use
    # fee_bps_assumption. Added after policies existed: hashed only when set (policy_hash).
    venue_fees: tuple[VenueFee, ...] = ()

    approval_ttl_seconds: int = Field(ge=60, le=86400)
    intent_ttl_seconds: int = Field(ge=60, le=7 * 86400)
    reconciliation_max_age_seconds: int = Field(ge=60, le=86400)
    blackout_windows: tuple[BlackoutWindow, ...] = ()

    risk_per_trade_pct: Pct = Field(gt=0, le=10)
    uncertainty_haircut_pct: Pct = Field(ge=0, lt=100)
    protective_retry_max: int = Field(ge=0, le=5)
    emergency: EmergencyPolicy = EmergencyPolicy()

    @field_validator("max_leverage")
    @classmethod
    def _no_leverage(cls, v: Decimal) -> Decimal:
        if v != 1:
            raise ValueError("leverage is not supported (spot only, max_leverage must be 1)")
        return v

    @field_validator("venue_fees")
    @classmethod
    def _one_fee_per_venue(cls, v: tuple[VenueFee, ...]) -> tuple[VenueFee, ...]:
        venues = [f.venue for f in v]
        if len(set(venues)) != len(venues):
            raise ValueError("venue_fees lists a venue twice")
        return tuple(sorted(v, key=lambda f: f.venue))

    def fee_bps_for(self, venue: str) -> Decimal:
        """The fee assumption for one venue: its own entry, else the general assumption."""
        return next((f.bps for f in self.venue_fees if f.venue == venue), self.fee_bps_assumption)

    @model_validator(mode="after")
    def _consistent(self) -> RiskPolicyParams:
        if self.max_order_notional > self.max_position_notional:
            raise ValueError("max_order_notional cannot exceed max_position_notional")
        if self.max_position_notional > self.max_gross_exposure:
            raise ValueError("max_position_notional cannot exceed max_gross_exposure")
        if self.approval_ttl_seconds > self.intent_ttl_seconds:
            raise ValueError("approval TTL cannot exceed intent TTL")
        return self


# New installations only (an existing policy is never replaced). Binance is the paper venue
# (ADR-027); Coinbase entries are inert once COINBASE is retired (its instruments are then not
# tradable) and keep the recorded test fixtures valid.
DEFAULT_POLICY = RiskPolicyParams(
    allowed_instruments=(
        "SPOT:BTC/USDT@BINANCE",
        "SPOT:PAXG/USDT@BINANCE",
        "SPOT:BTC/USD@COINBASE",
        "SPOT:ETH/USD@COINBASE",
    ),
    allowed_venues=("BINANCE", "COINBASE"),
    allowed_quote_currencies=("USDT", "USD"),
    max_order_notional=Decimal(500),
    max_position_notional=Decimal(1000),
    max_gross_exposure=Decimal(2000),
    max_concentration_pct=Decimal(50),
    max_daily_loss=Decimal(100),
    max_drawdown_pct=Decimal(10),
    max_loss_streak=4,
    max_open_orders=10,
    max_price_age_seconds=120,
    allow_delayed_data=True,  # paper only; live requires explicit operator acceptance (TRD-39)
    max_spread_bps=Decimal(25),
    max_cross_source_deviation_bps=Decimal(50),
    max_slippage_bps=Decimal(20),
    max_cost_bps=Decimal(100),
    rounding_tolerance_bps=Decimal(10),
    fee_bps_assumption=Decimal(60),  # ASSUMPTION pending venue fee confirmation (B-08)
    approval_ttl_seconds=900,
    intent_ttl_seconds=3600,
    reconciliation_max_age_seconds=900,
    risk_per_trade_pct=Decimal(1),
    uncertainty_haircut_pct=Decimal(25),
    protective_retry_max=2,
)

LIMIT_SEMANTICS: dict[str, dict[str, str]] = {
    "max_order_notional": {
        "measure": "quantity × approved limit price",
        "currency": "quote currency (USD)",
        "boundary": "per order",
        "fees": "excluded (checked separately in max_cost_bps)",
        "unrealised": "n/a",
        "unavailable": "block new exposure",
    },
    "max_position_notional": {
        "measure": "post-trade position quantity × reference price (limit price for the new order)",
        "currency": "USD",
        "boundary": "per instrument, point in time",
        "fees": "excluded",
        "unrealised": "marked at latest fresh quote",
        "unavailable": "block new exposure",
    },
    "max_gross_exposure": {
        "measure": "Σ |position × mark| + open exposure-increasing orders + this order",
        "currency": "USD",
        "boundary": "per account, point in time",
        "fees": "excluded",
        "unrealised": "marked at latest fresh quote",
        "unavailable": "block new exposure",
    },
    "max_concentration_pct": {
        "measure": "post-trade position notional / account equity",
        "currency": "USD",
        "boundary": "point in time",
        "fees": "excluded",
        "unrealised": "included in equity",
        "unavailable": "block new exposure",
    },
    "max_daily_loss": {
        "measure": "−(realised P&L since 00:00 UTC + current unrealised P&L), fees included",
        "currency": "USD",
        "boundary": "UTC calendar day (crypto trades 24/7)",
        "fees": "included",
        "unrealised": "included",
        "unavailable": "block new exposure",
    },
    "max_drawdown_pct": {
        "measure": "(high-water mark − equity) / high-water mark from equity snapshots",
        "currency": "USD",
        "boundary": "since account creation, never resets automatically",
        "fees": "included",
        "unrealised": "included",
        "unavailable": "block new exposure",
    },
    "max_loss_streak": {
        "measure": "consecutive closed round trips with negative realised P&L",
        "currency": "count",
        "boundary": "resets on a winning round trip",
        "fees": "included",
        "unrealised": "excluded",
        "unavailable": "block new exposure",
    },
}


# Fields added after policies were stored. Left out of the hash while unset, so the hashes of
# existing policy versions stay valid (active_policy re-checks them and fails closed).
HASHED_WHEN_SET = ("venue_fees",)


def policy_hash(params: RiskPolicyParams) -> str:
    data = params.model_dump(mode="python")
    for key in HASHED_WHEN_SET:
        if not data[key]:
            del data[key]
    return canonical_hash(data)


async def active_policy(conn: AsyncConnection) -> tuple[int, RiskPolicyParams]:
    row = (
        await conn.execute(select(risk_policies).where(risk_policies.c.active.is_(True)))
    ).first()
    if row is None:
        raise ConflictError("no active risk policy; risk checks cannot pass (fail closed)")
    try:
        params = RiskPolicyParams.model_validate(row.params)
    except ValueError:
        raise ConflictError("active risk policy is invalid (fail closed)") from None
    if policy_hash(params) != row.params_hash:
        raise ConflictError("active risk policy failed its integrity check (fail closed)")
    return int(row.version), params


async def ensure_default_policy(conn: AsyncConnection, now: datetime) -> int:
    """Create version 1 if no policy exists. Returns the active version."""
    existing = (
        await conn.execute(select(risk_policies.c.version).where(risk_policies.c.active.is_(True)))
    ).first()
    if existing is not None:
        return int(existing.version)
    await _insert(
        conn,
        version=1,
        params=DEFAULT_POLICY,
        reason="default conservative paper policy",
        created_by="system",
        now=now,
    )
    await record(
        conn,
        action="risk_policy.created",
        actor=Actor.system("policy"),
        occurred_at=now,
        entity_type="risk_policy",
        entity_id="1",
        payload={"version": 1},
    )
    return 1


async def _insert(
    conn: AsyncConnection,
    *,
    version: int,
    params: RiskPolicyParams,
    reason: str,
    created_by: str,
    now: datetime,
) -> None:
    await conn.execute(
        risk_policies.insert().values(
            id=uuid7(),
            version=version,
            params=to_jsonable(params.model_dump(mode="python")),
            params_hash=policy_hash(params),
            active=True,
            reason=reason,
            created_by=created_by,
            created_at=now,
        )
    )


ALLOWLISTS = ("allowed_instruments", "allowed_venues", "allowed_quote_currencies")


async def check_new_allowlist_entries(
    conn: AsyncConnection, current: RiskPolicyParams | None, new: RiskPolicyParams
) -> None:
    """Entries added to an allowlist must exist, an added instrument must be tradable, and its
    venue and quote currency must be allowed too (otherwise R04 refuses every proposal). A fee
    may only be set for a known venue (a misspelt one would silently change nothing)."""
    old = current.model_dump() if current is not None else {}
    added = {k: [v for v in getattr(new, k) if v not in old.get(k, ())] for k in ALLOWLISTS}
    problems: list[str] = []
    fee_venues = [f.venue for f in new.venue_fees]
    known_venues = {
        r.key
        for r in await conn.execute(
            select(venues.c.key).where(venues.c.key.in_(added["allowed_venues"] + fee_venues))
        )
    }
    problems += [f"unknown venue {v}" for v in added["allowed_venues"] if v not in known_venues]
    problems += [f"fee for unknown venue {v}" for v in fee_venues if v not in known_venues]
    known_assets = {
        r.symbol
        for r in await conn.execute(
            select(assets.c.symbol).where(assets.c.symbol.in_(added["allowed_quote_currencies"]))
        )
    }
    problems += [
        f"unknown currency {c}" for c in added["allowed_quote_currencies"] if c not in known_assets
    ]
    quote = assets.alias("quote")
    rows = {
        r.canonical_key: r
        for r in await conn.execute(
            select(
                instruments.c.canonical_key, instruments.c.tradable, venues.c.key, quote.c.symbol
            )
            .join(venues, venues.c.id == instruments.c.venue_id)
            .join(quote, quote.c.id == instruments.c.quote_asset_id)
            .where(instruments.c.canonical_key.in_(added["allowed_instruments"]))
        )
    }
    for key in added["allowed_instruments"]:
        r = rows.get(key)
        if r is None:
            problems.append(f"unknown instrument {key}")
        elif not r.tradable:
            problems.append(f"{key} is not tradable (no adapter trades it)")
        else:
            if r.key not in new.allowed_venues:
                problems.append(f"{key} also needs venue {r.key} allowed")
            if r.symbol not in new.allowed_quote_currencies:
                problems.append(f"{key} also needs currency {r.symbol} allowed")
    if problems:
        raise ValidationFailedError("; ".join(problems))


async def change_policy(
    conn: AsyncConnection,
    *,
    principal: Principal,
    new_params: dict[str, Any],
    reason: str,
    now: datetime,
    settings: Settings,
) -> int:
    """Separate authenticated workflow (TRD-11): capability, fresh MFA, reason.

    Non-retroactive: approvals bind the policy version, so a change invalidates them.
    """
    require(principal, Capability.CHANGE_RISK_LIMITS, now=now, settings=settings)
    if len(reason.strip()) < 10:
        raise ValidationFailedError("a reason of at least 10 characters is required")
    try:
        params = RiskPolicyParams.model_validate(new_params)
    except ValueError as exc:
        raise ValidationFailedError(f"invalid policy: {exc}") from None
    current = (
        await conn.execute(
            select(risk_policies).where(risk_policies.c.active.is_(True)).with_for_update()
        )
    ).first()
    if current is not None and current.params_hash == policy_hash(params):
        raise ConflictError("the new policy is identical to the active one")
    await check_new_allowlist_entries(
        conn,
        RiskPolicyParams.model_validate(current.params) if current is not None else None,
        params,
    )
    version = (current.version + 1) if current is not None else 1
    if current is not None:
        await conn.execute(
            update(risk_policies).where(risk_policies.c.id == current.id).values(active=False)
        )
    await _insert(
        conn,
        version=version,
        params=params,
        reason=reason.strip(),
        created_by=str(principal.user_id),
        now=now,
    )
    await record(
        conn,
        action="risk_policy.changed",
        actor=principal.actor,
        occurred_at=now,
        entity_type="risk_policy",
        entity_id=str(version),
        payload={
            "from_version": current.version if current else None,
            "to_version": version,
            "reason": reason,
        },
    )
    return version
