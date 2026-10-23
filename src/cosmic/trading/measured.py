"""What the venues really charged: the commission per side, measured from the owner's fills.

The risk policy's fee per venue is an assumption until the owner confirms it (B-08), and it
drives sizing, the cost check, every test and the decision guide. The fills already hold what
each venue charged, so the system can measure it:

- only entry fills count: an exit fill on a CFD also carries the overnight swap;
- fills of the paper simulator are left out: it charges the policy's own assumption, so it
  would only echo it;
- a fee in the base asset (Binance) is valued at the fill price; a fee in another asset is
  left out (it cannot be valued here).

Read-only: changing the policy stays the owner's decision, with a fresh code.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.money import ZERO
from cosmic.db.schema_execution import fills, orders
from cosmic.db.schema_market import instruments, venues
from cosmic.db.schema_trading import accounts

SIMULATOR = "paper"
NOTICEABLE_BPS = Decimal(5)  # a measured fee this far from the policy's is worth telling


@dataclass(frozen=True)
class MeasuredFee:
    venue: str
    fills: int
    notional: Decimal  # the entry fills' value, in the quote currency
    fees: Decimal  # what the venue charged on them

    @property
    def bps(self) -> Decimal:
        """The commission per side, in bps of the trade's value."""
        if self.notional <= ZERO:
            return ZERO
        return (self.fees / self.notional * 10_000).quantize(Decimal("0.01"))


async def measured_fees(conn: AsyncConnection) -> dict[str, MeasuredFee]:
    """The commission each real venue charged on the owner's entry fills, by venue key."""
    rows = await conn.execute(
        select(
            venues.c.key,
            fills.c.quantity,
            fills.c.price,
            fills.c.fee,
            fills.c.fee_currency,
            instruments.c.pair_key,
        )
        .select_from(
            fills.join(orders, orders.c.id == fills.c.order_id)
            .join(accounts, accounts.c.id == orders.c.account_id)
            .join(venues, venues.c.id == accounts.c.venue_id)
            .join(instruments, instruments.c.id == orders.c.instrument_id)
        )
        .where(orders.c.purpose == "ENTRY", accounts.c.broker_adapter != SIMULATOR)
    )
    totals: dict[str, list[Decimal]] = {}
    for r in rows:
        base, _, quote = r.pair_key.partition("/")
        if r.fee_currency == quote:
            fee = r.fee
        elif r.fee_currency == base:
            fee = r.fee * r.price
        else:
            continue
        t = totals.setdefault(r.key, [ZERO, ZERO, ZERO])
        t[0] += 1
        t[1] += r.quantity * r.price
        t[2] += fee
    return {k: MeasuredFee(k, int(n), value, fee) for k, (n, value, fee) in totals.items()}


def advice(measured: MeasuredFee, policy_bps: Decimal) -> str | None:
    """A plain sentence when the measured fee is far from the policy's assumption."""
    if measured.fills == 0 or abs(measured.bps - policy_bps) < NOTICEABLE_BPS:
        return None
    return (
        f"Your {measured.venue} trades show a commission of {measured.bps:.2f} bps a side over "
        f"{measured.fills} fill(s), but the risk policy assumes {policy_bps:.2f} bps. Set Fee "
        f"per venue {measured.venue}={measured.bps.normalize():f} under Risk policy if that is "
        "your account's fee."
    )
