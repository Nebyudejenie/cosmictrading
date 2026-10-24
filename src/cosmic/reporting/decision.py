"""Should I take this trade? A decision guide for every proposal to buy (ADR-033).

Six signs, each GOOD, CAUTION, AGAINST or UNKNOWN, from data the system already holds:

1. **Evidence:** what the rule competition (ADR-031) says about the proposal's rule on this
   market: earned, qualified, promising, or no proven edge.
2. **Trend:** with or against the daily trend of the market's signal series (close against its
   50-day average).
3. **Time:** the trading session now (London and New York carry most of the volume).
4. **Cost now:** the spread now against its median over the last day.
5. **Cost of this trade:** what spread, fees and slippage take of the trade's risk, in R.
6. **Size:** the loss at the stop as a share of the account's equity.

It informs the owner's decision and changes nothing: it is shown on the approval page and with
the Telegram card. It is a guide, not advice, and no sign promises a result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import NotFoundError
from cosmic.core.labels import DataLabel
from cosmic.market import data as md
from cosmic.market.indicators import sma
from cosmic.research.library import compete_store, expect, insight
from cosmic.trading import mandates

MARKS = {"GOOD": "✅", "CAUTION": "⚠️", "AGAINST": "❌", "UNKNOWN": "❔"}
SPREAD_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class Sign:
    name: str
    mark: str  # GOOD | CAUTION | AGAINST | UNKNOWN
    text: str

    @property
    def icon(self) -> str:
        return MARKS[self.mark]


@dataclass(frozen=True)
class Guide:
    signs: list[Sign] = field(default_factory=list)

    def count(self, mark: str) -> int:
        return sum(1 for s in self.signs if s.mark == mark)

    @property
    def summary(self) -> str:
        if not self.signs:
            return ""
        head = (
            f"{self.count('GOOD')} of {len(self.signs)} signs are fine, "
            f"{self.count('CAUTION')} call for care, {self.count('AGAINST')} are against."
        )
        evidence = next((s for s in self.signs if s.name == "Evidence"), None)
        if evidence is not None and evidence.mark in ("AGAINST", "UNKNOWN"):
            return head + " The rule has no proven edge yet: treat it as a demo test, not a bet."
        if self.count("AGAINST"):
            return head + " Waiting for a better moment is a decision too."
        return head + " Still no promise: the stop limits the loss, nothing limits the market."


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v is not None else None
    except (InvalidOperation, ValueError):
        return None


def timeframe_of(intent: Any) -> str:
    """The bars a proposal's rule trades on: as recorded with it (the auto-trader names them
    since addendum 5); before that the auto-trader's were hourly (it names its mandate) and the
    strategy library's daily."""
    details = intent.details or {}
    if details.get("timeframe") in ("1h", "1d"):
        return str(details["timeframe"])
    return "1h" if details.get("mandate_id") else "1d"


async def _evidence(rule: str | None, market: str, timeframe: str, conn: AsyncConnection) -> Sign:
    if not rule:
        return Sign("Evidence", "UNKNOWN", "A trade without a rule: there is no record to check.")
    run = await compete_store.latest_run(conn)
    if run is None:
        return Sign("Evidence", "UNKNOWN", "The rule competition has not run yet.")
    # Only this market's own record: another market, even on the same prices, was scored
    # with its own fees, spread and swap.
    rows = [e for e in run.find(rule, timeframe) if e["market"] == market]
    if not rows:
        bars = "hourly" if timeframe == "1h" else "daily"
        return Sign(
            "Evidence", "UNKNOWN", f"This rule is not tested on this market's {bars} prices."
        )
    e = rows[0]
    val, fwd = e["val"], e["forward"]
    unseen = f"{val['mean_r']:+.2f} R a trade" if val.get("mean_r") is not None else "no trades"
    forward = f"; {fwd['trades']} forward trades, {fwd['total_r']:+.1f} R" if fwd["trades"] else ""
    if e.get("earned"):
        return Sign(
            "Evidence",
            "GOOD",
            f"It earned the auto-trader: proven on unseen prices and forward{forward}.",
        )
    if e["tier"] == "QUALIFIED":
        return Sign(
            "Evidence",
            "CAUTION",
            f"It passed on unseen prices ({unseen}); its forward record is short{forward}.",
        )
    if e["tier"] == "PROMISING":
        return Sign(
            "Evidence",
            "CAUTION",
            f"It made money on unseen prices ({unseen}), not yet clearly beyond luck{forward}.",
        )
    return Sign(
        "Evidence", "AGAINST", f"No proven edge: on prices it never saw it made {unseen}{forward}."
    )


async def _trend(conn: AsyncConnection, signal: str) -> Sign:
    try:
        inst = await md.instrument_by_key(conn, signal)
    except NotFoundError:
        return Sign("Trend", "UNKNOWN", "No daily prices for this market.")
    bars = await md.complete_bars(conn, inst.id, "1d", limit=200)
    closes = [float(b.close) for b in bars]
    avg = sma(closes, 50)[-1] if len(closes) >= 50 else None
    if avg is None:
        return Sign("Trend", "UNKNOWN", "Not enough daily prices for the 50-day average.")
    gap = (closes[-1] / avg - 1) * 100
    if closes[-1] > avg:
        return Sign(
            "Trend",
            "GOOD",
            f"With the daily trend: the price is {gap:+.1f} % against its 50-day average.",
        )
    return Sign(
        "Trend",
        "CAUTION",
        f"Against the daily trend: the price is {gap:+.1f} % below its 50-day average.",
    )


def _time(now: datetime) -> Sign:
    s = insight.session_of(now)
    if s in ("London", "New York"):
        return Sign("Time", "GOOD", f"{s} session: the busiest hours, with the tightest prices.")
    return Sign(
        "Time", "CAUTION", f"{s} session: quieter hours, often wider prices and sudden moves."
    )


async def _cost_now(conn: AsyncConnection, key: str, now: datetime) -> tuple[Sign, Decimal | None]:
    try:
        inst = await md.instrument_by_key(conn, key)
    except NotFoundError:
        return Sign("Cost now", "UNKNOWN", "No prices for this market."), None
    q = await md.latest_quote(conn, inst.id, now)
    usual = await md.median_spread(conn, inst.id, now, SPREAD_WINDOW)
    if q.label not in (DataLabel.LIVE, DataLabel.DELAYED):
        return Sign("Cost now", "UNKNOWN", f"No fresh price ({q.label.value})."), None
    if q.bid is None or q.ask is None or q.bid <= 0:
        return Sign("Cost now", "UNKNOWN", "No fresh price."), None
    spread = (q.ask - q.bid) / ((q.ask + q.bid) / 2)
    if usual is None or usual <= 0:
        return Sign(
            "Cost now",
            "UNKNOWN",
            f"The spread is {spread * 100:.3f} %; its usual level is not known yet.",
        ), spread
    ratio = spread / usual
    text = f"The spread is {spread * 100:.3f} %, {ratio:.1f}× its usual {usual * 100:.3f} %."
    if ratio <= Decimal("1.5"):
        return Sign("Cost now", "GOOD", text), spread
    if ratio <= 3:
        return Sign("Cost now", "CAUTION", text + " Trading now costs more than usual."), spread
    return Sign("Cost now", "AGAINST", text + " Waiting for a normal spread is cheaper."), spread


def _cost_of_trade(packet: dict[str, Any], spread: Decimal | None) -> Sign:
    entry = _dec(packet["order"].get("limit_price"))
    stop = _dec(((packet.get("management_plan") or {}).get("stop_loss") or {}).get("stop_price"))
    fee = (_dec(packet["costs"].get("fee_bps")) or Decimal(0)) / 10_000
    if entry is None or stop is None or entry <= stop:
        return Sign(
            "Cost of this trade", "UNKNOWN", "No stop, so the cost cannot be set against the risk."
        )
    slip = Decimal(str(expect.SLIPPAGE))
    cost = entry * ((spread or Decimal(0)) + 2 * fee + 2 * slip)
    risk = entry - stop + 2 * entry * fee
    r = cost / risk
    text = (
        f"Spread, fees and slippage take about {r:.2f} R ({r * 100:.0f} % of what the stop risks)"
        + ("." if spread is not None else ", without the spread (unknown).")
    )
    if r <= Decimal("0.10"):
        return Sign("Cost of this trade", "GOOD", text)
    if r <= Decimal("0.25"):
        return Sign("Cost of this trade", "CAUTION", text)
    return Sign(
        "Cost of this trade", "AGAINST", text + " The move must be large just to break even."
    )


def _size(packet: dict[str, Any]) -> Sign:
    risk = packet.get("risk") or {}
    loss, pct = (
        _dec(risk.get("estimated_max_loss")),
        _dec(risk.get("estimated_max_loss_pct_of_equity")),
    )
    if loss is None:
        return Sign("Size", "UNKNOWN", "The loss at the stop is not known.")
    ccy = packet["costs"].get("currency", "")
    share = f" ({pct:.2f} % of the account)" if pct is not None else ""
    text = f"At the stop it loses at most about {loss:,.2f} {ccy}{share}."
    if pct is None or pct <= 1:
        return Sign("Size", "GOOD", text)
    if pct <= 2:
        return Sign("Size", "CAUTION", text)
    return Sign("Size", "AGAINST", text + " That is a large share of the account for one trade.")


async def guide(
    conn: AsyncConnection, packet: dict[str, Any], now: datetime, timeframe: str = "1h"
) -> Guide:
    """The decision guide for a proposal's packet; empty for an exit, which only reduces risk.
    ``timeframe`` is the bars its rule trades on (``timeframe_of``)."""
    if packet["order"].get("side") != "BUY":
        return Guide()
    key = packet["instrument"]["canonical_key"]
    signal = mandates.AUTO_SIGNAL_SERIES.get(key, key)
    rule = (packet.get("strategy") or {}).get("key")
    cost_now, spread = await _cost_now(conn, key, now)
    return Guide(
        [
            await _evidence(rule, key, timeframe, conn),
            await _trend(conn, signal),
            _time(now),
            cost_now,
            _cost_of_trade(packet, spread),
            _size(packet),
        ]
    )


def telegram_lines(g: Guide) -> list[str]:
    if not g.signs:
        return []
    return [
        "Decision guide (not advice):",
        *(f"{s.icon} {s.name}: {s.text}" for s in g.signs),
        g.summary,
    ]
