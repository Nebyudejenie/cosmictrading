"""HFM's actual costs on the owner's demo account (S65), from HFM's own records in the bridge's
report, never assumed (the owner, 2026-11-05: "Verify the actual account commission, spread,
swap and other costs. Do not assume HFM commission is zero").

- **Commission and fees:** what HFM charged on each of its deals (commission + fee), against the
  deal's value: per side, in basis points. Only measured from at least ``MIN_DEALS`` deals.
- **Spread:** HFM's ask less its bid now; it is paid inside the price of every trade.
- **Swap:** HFM's overnight terms for gold, per ounce a night at today's price."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.money import ZERO
from cosmic.db.schema_trading import bridge_state
from cosmic.gold import sim

MIN_DEALS = 5  # fewer deals say too little about what HFM charges
DEAL_TRADES = (0, 1)  # MT5 deal types BUY and SELL (not balance, credit or the like)
BPS = Decimal(10_000)


@dataclass(frozen=True)
class HfmCosts:
    deals: int  # HFM's trade deals measured
    charged: Decimal  # commission and fees HFM charged on them, in the account currency
    value: Decimal  # the deals' value
    spread: Decimal | None  # the ask less the bid now, an ounce
    swap_buy: Decimal | None  # an ounce a night, at today's price (negative: paid)
    swap_sell: Decimal | None

    @property
    def measured(self) -> bool:
        return self.deals >= MIN_DEALS

    @property
    def bps(self) -> Decimal:
        """Commission and fees per side, in basis points of the trade's value."""
        if self.value <= ZERO:
            return ZERO
        return (self.charged / self.value * BPS).quantize(Decimal("0.01"))


def _d(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


async def measure(conn: AsyncConnection) -> HfmCosts | None:
    """HFM's costs from its latest report, or None when the bridge has never reported."""
    row = (
        await conn.execute(
            select(bridge_state.c.snapshot).where(bridge_state.c.bridge == sim.BRIDGE)
        )
    ).first()
    if row is None:
        return None
    snap = row.snapshot or {}
    terms = (snap.get("symbols") or {}).get("XAUUSD") or {}
    contract = _d(terms.get("contract_size")) or Decimal(100)
    deals = charged = value = ZERO
    for d in snap.get("deals") or ():
        if d.get("type") not in DEAL_TRADES or d.get("symbol", "XAUUSD") != "XAUUSD":
            continue
        if d.get("commission") is None and d.get("fee") is None:
            continue  # nothing said about what it cost: not counted either way
        deals += 1
        charged += -(_d(d.get("commission")) + _d(d.get("fee")))  # MT5 reports a charge < 0
        value += _d(d.get("volume")) * contract * _d(d.get("price"))
    tick = (snap.get("ticks") or {}).get("XAUUSD") or {}
    bid, ask = tick.get("bid"), tick.get("ask")
    spread = _d(ask) - _d(bid) if bid is not None and ask is not None else None
    swap_buy = sim.swap_per_ounce(terms, "BUY", _d(bid)) if bid is not None and terms else None
    swap_sell = sim.swap_per_ounce(terms, "SELL", _d(ask)) if ask is not None and terms else None
    return HfmCosts(int(deals), charged, value, spread, swap_buy, swap_sell)


def fee_bps(policy: Any, costs: HfmCosts | None, venue: str = "HFM") -> tuple[Decimal, str]:
    """The fee the HFM figures use, and where it comes from: the risk policy's own entry for
    HFM when there is one; else HFM's measured charge; else the policy's general assumption."""
    if any(f.venue == venue for f in policy.venue_fees):
        return policy.fee_bps_for(venue), "the risk policy's fee for HFM"
    if costs is not None and costs.measured:
        return costs.bps, f"measured from {costs.deals} of HFM's own deals"
    return policy.fee_bps_for(venue), "the risk policy's general assumption (not measured yet)"
