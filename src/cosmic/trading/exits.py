"""Owner-requested exits (TRD-22): close a position by replacing its entries' protective stops.

The proposal names every entry that still holds part of the position, so approving it cancels
their stops first and then sells — immediately or not at all (IOC). If it does not fill, the
stops are placed again. Nothing is sold without the owner approving these exact terms.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.core.errors import ValidationFailedError
from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.schema_trading import positions
from cosmic.market import data as md
from cosmic.trading.accounts import Account
from cosmic.trading.proposals import (
    Candidate,
    ProposalResult,
    create_candidate,
    entry_available,
    open_entries,
)

BAND = Decimal("0.01")  # ±1 %: room for a human-paced approval
UNDER_BID = Decimal("0.999")  # marketable: 0.1 % under the bid


def _tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(ROUND_DOWN) * tick


async def propose_close(
    conn: AsyncConnection,
    *,
    account: Account,
    inst: md.InstrumentView,
    reason: str,
    actor: Actor,
    now: datetime,
) -> ProposalResult:
    reason = reason.strip()
    if len(reason) < 5:
        raise ValidationFailedError("give a reason of at least 5 characters")
    held = Decimal(
        str(
            await conn.scalar(
                select(positions.c.quantity).where(
                    positions.c.account_id == account.id, positions.c.instrument_id == inst.id
                )
            )
            or 0
        )
    )
    if held <= ZERO:
        raise ValidationFailedError(f"{account.alias} holds no {inst.canonical_key}")
    q = await md.latest_quote(conn, inst.id, now)
    if q.label is not DataLabel.DELAYED or q.bid is None:
        raise ValidationFailedError(
            f"no fresh quote for {inst.canonical_key} ({q.label.value}); try again in a minute"
        )
    owners = await open_entries(conn, account.id, inst.id)
    entries = [(r, await entry_available(conn, r.id)) for r in owners]
    entries = [(r, avail) for r, avail in entries if avail > ZERO]
    if owners and not entries:
        raise ValidationFailedError("an exit for this position is already in progress")
    qty = min(sum((a for _, a in entries), ZERO), held) if entries else held
    tick = inst.tick_size or Decimal("0.01")
    limit = _tick(q.bid * UNDER_BID, tick)
    codes = ", ".join(r.trade_code for r, _ in entries)
    replaces = (
        f"The protective stops of {codes} are cancelled first and come back if the sale does "
        "not fill."
        if entries
        else "No protective stop belongs to this position."
    )
    candidate = Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": inst.canonical_key,
            "side": "SELL",
            "effect": "CLOSE" if qty >= held else "REDUCE",
            "quantity": qty,
            "limit_price": limit,
            "price_band_low": _tick(q.bid * (1 - BAND), tick),
            "price_band_high": _tick(q.bid * (1 + BAND), tick),
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",
            "horizon": "immediate",
            "thesis": (
                f"Owner-requested exit: {reason}. Sells {qty} {inst.base} at or above {limit} "
                f"(0.1% under the bid), immediately or not at all. {replaces}"
            )[:4000],
            "plain_summary": f"Close {qty} {inst.base} on {account.alias}: {reason}"[:600].ljust(
                20, "."
            ),
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"{q.source_key}:{inst.canonical_key}",
                    "summary": f"bid {q.bid} / ask {q.ask} ({q.label.value})",
                    "source_ts": q.source_ts,
                },
                {"kind": "NOTE", "ref": "operator", "summary": f"exit requested: {reason}"[:500]},
            ],
            "counterarguments": [
                "The price may rise after the sale.",
                "Selling at the bid pays the spread and fees (ASSUMPTION).",
            ],
            "red_team": (
                "Exiting on a short-term move can lock in a loss that would have recovered."
            ),
            "scenarios": [
                {
                    "name": "Exit fills",
                    "description": "The position is sold at or above the limit.",
                },
                {
                    "name": "No fill",
                    "description": "The order expires at once; the stop is placed again.",
                },
            ],
            "invalidation": ["Not applicable to an exit"],
            "entry_conditions": f"Limit sell at or above {limit}, immediate or cancel",
            "closes": [r.id for r, _ in entries],
        }
    )
    return await create_candidate(conn, candidate=candidate, actor=actor, now=now)
