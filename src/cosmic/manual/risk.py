"""The owner's risk rules for their manual trades (S113), and the checks of one trade against
them, the real account and the market now.

The owner's specification (2026-11-22): 1 % a trade by default, at most 2 % unless they change
their approved rules (with their code, never above the 10 % of their PDF plan); a daily loss
limit; a limit on the total risk open; on positions at once; a cooling-off after losses in a
row; a daily number of trades (their plan: 2, Monday to Friday); no duplicate order; a warning
when a trade adds to the same direction. A trade over a limit is refused here, with why. The
size is never raised after a loss: nothing here ever proposes more risk than the setting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import record as audit
from cosmic.core.config import Settings
from cosmic.core.errors import ValidationFailedError
from cosmic.core.jsonutil import to_jsonable
from cosmic.db.schema_manual import manual_journal, manual_risk_settings
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require
from cosmic.manual import book
from cosmic.manual.calc import BLOCK, OK, WARN, Line, Sizing, Ticket, money
from cosmic.manual.live import BUY, LiveAccount, Spec

ABSOLUTE_MAX_PCT = Decimal(10)  # the owner's PDF plan's own maximum; never more, whatever is set


class RiskRules(BaseModel):
    """The owner's approved rules (one version per change)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    risk_pct_default: Decimal = Field(default=Decimal(1), gt=0, le=ABSOLUTE_MAX_PCT)
    risk_pct_max: Decimal = Field(default=Decimal(2), gt=0, le=ABSOLUTE_MAX_PCT)
    daily_loss_pct: Decimal = Field(default=Decimal(3), gt=0, le=50)
    max_open_risk_pct: Decimal = Field(default=Decimal(4), gt=0, le=50)
    max_positions: int = Field(default=2, ge=1, le=20)
    max_losses_in_a_row: int = Field(default=3, ge=1, le=20)
    cooling_off_hours: int = Field(default=24, ge=0, le=168)
    daily_trades: int = Field(default=2, ge=1, le=50)
    weekdays_only: bool = True
    duplicate_minutes: int = Field(default=5, ge=0, le=120)

    @model_validator(mode="after")
    def _default_within_max(self) -> RiskRules:
        if self.risk_pct_default > self.risk_pct_max:
            raise ValueError("the default risk cannot be above the maximum")
        return self


DEFAULT = RiskRules()


async def current(conn: AsyncConnection) -> tuple[int, RiskRules]:
    """The latest approved version, or the specification's defaults (version 0)."""
    r = (
        await conn.execute(
            select(manual_risk_settings).order_by(desc(manual_risk_settings.c.version)).limit(1)
        )
    ).first()
    if r is None:
        return 0, DEFAULT
    return r.version, RiskRules.model_validate(r.params)


async def change(
    conn: AsyncConnection,
    *,
    principal: Principal,
    params: dict[str, Any],
    reason: str,
    now: datetime,
    settings: Settings,
) -> int:
    """A new version of the owner's rules (their code: CHANGE_RISK_LIMITS needs it)."""
    require(principal, Capability.CHANGE_RISK_LIMITS, now=now, settings=settings)
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise ValidationFailedError("say why the rules change (up to 500 characters)")
    try:
        rules = RiskRules.model_validate(params)
    except ValueError as exc:
        raise ValidationFailedError(f"the rules are not valid: {exc}") from None
    version = int(
        await conn.scalar(select(func.coalesce(func.max(manual_risk_settings.c.version), 0))) or 0
    )
    version += 1
    await conn.execute(
        manual_risk_settings.insert().values(
            version=version,
            params=to_jsonable(rules.model_dump()),
            reason=reason,
            created_by=f"{principal.actor.type.value}:{principal.actor.id}",
            created_at=now,
        )
    )
    await audit(
        conn,
        action="manual.risk_rules_changed",
        actor=principal.actor,
        occurred_at=now,
        entity_type="manual_risk_settings",
        entity_id=str(version),
        payload={"rules": to_jsonable(rules.model_dump()), "reason": reason},
    )
    return version


def open_risk(account: LiveAccount, spec: Spec) -> tuple[Decimal, list[str]]:
    """The money the open positions lose if every stop-loss is hit, and those with none."""
    total, unlimited = Decimal(0), []
    vpp = spec.value_per_price
    for p in account.positions:
        if p.sl is None:
            unlimited.append(f"#{p.ticket}")
            continue
        move = (p.price_open - p.sl) if p.side == BUY else (p.sl - p.price_open)
        total += max(move, Decimal(0)) * p.lots * vpp
    return total, unlimited


@dataclass(frozen=True)
class Recent:
    """The owner's own journal of the last minutes (a trade placed but not yet in HFM's report)."""

    side: str
    at: datetime


async def recent_placed(conn: AsyncConnection, since: datetime) -> list[Recent]:
    rows = await conn.execute(
        select(manual_journal.c.side, manual_journal.c.created_at).where(
            manual_journal.c.kind == "PLACED", manual_journal.c.created_at >= since
        )
    )
    return [Recent(r.side, r.created_at) for r in rows]


def check(
    account: LiveAccount,
    ticket: Ticket,
    sizing: Sizing | None,
    rules: RiskRules,
    now: datetime,
    placed: Sequence[Recent] = (),
) -> list[Line]:
    """Every rule against this trade, the account and the market now (OK, WARN or BLOCK)."""
    lines: list[Line] = []
    if not account.live:
        lines.append(Line(BLOCK, f"no live account: {account.problem}"))
    if account.quote is None or not account.quote.fresh:
        lines.append(
            Line(
                BLOCK,
                "no current price from your live account: the market is closed or the "
                "terminal is not reporting, so nothing that needs the price is calculated",
            )
        )
    day = account.server_day(now)
    if rules.weekdays_only and (day + (account.server_offset or timedelta(0))).weekday() >= 5:
        lines.append(Line(BLOCK, "your plan trades Monday to Friday only (HFM's own day)"))
    equity = account.equity or Decimal(0)
    new_risk = sizing.risk_cash if sizing is not None and not sizing.blocked else Decimal(0)
    trades = book.closed_trades(account.deals)
    # Daily loss: realised today on HFM's day, and what is floating now.
    today = book.realised_since(account.deals, day) + (account.floating or Decimal(0))
    limit = equity * rules.daily_loss_pct / 100
    if account.equity is None:  # S115: no live equity, no limit to judge (never "0.00 reached")
        lines.append(
            Line(
                WARN,
                "your daily loss limit is not checked: your live equity is not known until "
                "your account is connected",
            )
        )
    elif -today >= limit:
        lines.append(
            Line(BLOCK, f"today's loss ({money(-today)}) reached your daily limit ({money(limit)})")
        )
    elif new_risk and -today + new_risk > limit:
        lines.append(
            Line(
                BLOCK,
                f"at its stop-loss this trade would take today's loss to "
                f"{money(-today + new_risk)}, past your daily limit of {money(limit)}",
            )
        )
    else:
        lines.append(Line(OK, f"today: {money(today):+} of a {money(limit)} daily loss limit"))
    # Open risk and positions.
    if account.spec is not None:
        held, unlimited = open_risk(account, account.spec)
        if unlimited:
            lines.append(
                Line(
                    BLOCK,
                    f"open position {', '.join(unlimited)} has no stop-loss: its risk "
                    "has no limit; set one in MT5 first",
                )
            )
        cap = equity * rules.max_open_risk_pct / 100
        if held + new_risk > cap:
            lines.append(
                Line(
                    BLOCK,
                    f"open risk would be {money(held + new_risk)}, above your limit of "
                    f"{money(cap)} ({rules.max_open_risk_pct.normalize():f} % of equity)",
                )
            )
        else:
            lines.append(
                Line(OK, f"open risk with this trade: {money(held + new_risk)} of {money(cap)}")
            )
    if len(account.positions) >= rules.max_positions:
        lines.append(
            Line(
                BLOCK,
                f"{len(account.positions)} positions are open: your limit is {rules.max_positions}",
            )
        )
    # Losses in a row: a cooling-off.
    streak = book.losses_in_a_row(trades)
    if streak >= rules.max_losses_in_a_row and trades:
        until = trades[-1].closed_at + timedelta(hours=rules.cooling_off_hours)
        if now < until:
            lines.append(
                Line(
                    BLOCK,
                    f"{streak} losing trades in a row: cooling off until "
                    f"{until:%d %b %H:%M} UTC, as your rules say",
                )
            )
    # Trades today.
    opened = max(book.opened_since(account.deals, day), sum(1 for p in placed if p.at >= day))
    if opened >= rules.daily_trades:
        lines.append(
            Line(BLOCK, f"{opened} trades today: your plan allows {rules.daily_trades} a day")
        )
    # A duplicate: the same direction just placed, or open at about the same price.
    window = timedelta(minutes=rules.duplicate_minutes)
    for r in placed:
        if r.side == ticket.side and now - r.at <= window:
            lines.append(
                Line(
                    BLOCK,
                    f"you recorded a {r.side} {int((now - r.at).total_seconds())} s ago: "
                    "this looks like a duplicate",
                )
            )
            break
    spread = account.quote.spread if account.quote is not None else Decimal(0)
    for p in account.positions:
        if p.side == ticket.side and abs(p.price_open - ticket.entry) <= max(
            3 * spread, Decimal("0.5")
        ):
            lines.append(
                Line(
                    BLOCK,
                    f"#{p.ticket} is already a {p.side} at {p.price_open}: this looks "
                    "like a duplicate",
                )
            )
        elif p.side == ticket.side:
            lines.append(
                Line(
                    WARN,
                    f"adds to your open {p.side} (#{p.ticket}): the same direction in the "
                    "same market, so the two lose together",
                )
            )
        else:
            lines.append(Line(WARN, f"against your open {p.side} (#{p.ticket}): a hedge"))
    # Never more risk to recover a loss.
    if (
        trades
        and trades[-1].net < 0
        and ticket.risk_pct
        and ticket.risk_pct > rules.risk_pct_default
    ):
        lines.append(
            Line(
                WARN,
                "more than your usual risk right after a loss: your rules never raise "
                "the size to win a loss back",
            )
        )
    return lines


def verdict(lines: Sequence[Line]) -> str:
    if any(x.level == BLOCK for x in lines):
        return BLOCK
    if any(x.level == WARN for x in lines):
        return WARN
    return OK
