"""Learning mode on the demo accounts (S124, the owner's choice of 2026-11-28, ADR-045).

The owner: "make the auto trader robots trade… start trade by themselves… find one logic, the
percentage of losing and winning and probability… learn… it is demo money". Their answers:

- **Every signal, by itself, on a demo:** on a demo account (SANDBOX, virtual money) each robot
  takes every valid signal of its own logic, with no approval, no 30 % line and no
  positive-expected-value gate. Its money limits and the risk checks still hold, and the broker
  holds every trade's stop-loss and take-profit. A real account never trades this way.
- **Stopped for the day:** three losing trades in a row, or the daily loss limit, stop a robot
  for the rest of the day (UTC) only; with the next day it trades again by itself.
- **One logic for its odds (this module):** each robot's chance of winning starts from its tested
  record (four years of HFM's gold) and moves to what its own demo trades show, one trade at a
  time: ``(W × record + won) / (W + trades)``, the record counting as ``W`` = 30 trades. Beside it
  the measured win and loss percentages with their 95 % range. LEARNING until 30 demo trades,
  then MEASURED; never called verified, and the rules themselves are never tuned on these
  results (a rule fitted to its own few trades only fools itself).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.money import ZERO
from cosmic.db.schema_execution import post_trade_reviews, trade_intents
from cosmic.notify.outbox import notify_owners
from cosmic.trading import mandates
from cosmic.trading.accounts import Account

KINDS = frozenset({"SANDBOX"})  # demo accounts only: virtual money
WEIGHT = 30  # the tested record counts as this many trades
MEASURED_AT = 30  # demo trades before the card says MEASURED instead of LEARNING
TOLD_WITHIN = timedelta(hours=6)  # a closed trade is told once, while it is this recent
PCT = Decimal("0.1")


@dataclass(frozen=True)
class Record:
    """A robot's tested record: the chance its target comes first, and its break-even."""

    robot: str
    p_win: Decimal
    break_even: Decimal
    where: str


# Robot B: the 1-minute rule's model (lab Run 6): 26.9 % on 2026, 33.3 % needed.
M1 = Record("Robot B", Decimal("0.269"), Decimal("0.333"), "HFM 1-minute gold, 2022-11 to 2026-11")
# Robot A: the trend rule, both sides (lab Run 8): BUY 36.6 % of 372, SELL 31.2 % of 224.
TREND = Record(
    "Robot A", Decimal("0.346"), Decimal("0.333"), "HFM 15-minute gold, 2022-11 to 2026-11"
)
RECORDS = {mandates.GOLD_M1: M1, mandates.GOLD_TREND_BUY: TREND}


def on(account: Account) -> bool:
    """Learning mode applies to this account: a demo, never a real one."""
    return account.kind in KINDS


def wilson(won: int, n: int, z: float = 1.96) -> tuple[Decimal, Decimal]:
    """The 95 % range of a win rate measured on ``n`` trades."""
    if n <= 0:
        return ZERO, Decimal(1)
    p = won / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (
        Decimal(str(round(max(centre - half, 0.0), 4))),
        Decimal(str(round(min(centre + half, 1.0), 4))),
    )


def learned(record: Decimal, won: int, trades: int) -> Decimal:
    """The robot's chance of winning: its record, moved by its own demo trades."""
    value = (WEIGHT * record + won) / (WEIGHT + trades)
    return value.quantize(Decimal("0.0001"), ROUND_HALF_UP)


def pct(x: Decimal) -> str:
    return f"{(x * 100).quantize(PCT, ROUND_HALF_UP)} %"


@dataclass(frozen=True)
class Card:
    """A robot's learning card: its demo trades since learning mode began."""

    record: Record
    trades: int
    won: int
    lost: int
    net: Decimal

    @property
    def even(self) -> int:
        return self.trades - self.won - self.lost

    @property
    def win_rate(self) -> Decimal | None:
        return Decimal(self.won) / self.trades if self.trades else None

    @property
    def loss_rate(self) -> Decimal | None:
        return Decimal(self.lost) / self.trades if self.trades else None

    @property
    def chance(self) -> Decimal:
        return learned(self.record.p_win, self.won, self.trades)

    @property
    def range(self) -> tuple[Decimal, Decimal] | None:
        return wilson(self.won, self.trades) if self.trades else None

    @property
    def label(self) -> str:
        if self.trades >= MEASURED_AT:
            return f"MEASURED · DEMO · {self.trades} trades"
        return f"LEARNING · DEMO · {self.trades} of {MEASURED_AT} trades"

    def line(self) -> str:
        """One sentence, for the owner's messages and the page."""
        start = f"tested record {pct(self.record.p_win)} ({self.record.where})"
        if not self.trades:
            return (
                f"{self.record.robot}: no demo trade closed yet; chance of winning "
                f"{pct(self.chance)}, its {start}; break-even {pct(self.record.break_even)}"
            )
        low, high = self.range or (ZERO, Decimal(1))
        assert self.win_rate is not None and self.loss_rate is not None
        return (
            f"{self.record.robot}: {self.trades} demo trade(s): won {self.won} "
            f"({pct(self.win_rate)}), lost {self.lost} ({pct(self.loss_rate)}), net "
            f"{self.net:+,.2f} USD; its win rate is between {pct(low)} and {pct(high)} (95 %); "
            f"chance of winning now {pct(self.chance)}, learned from its {start}; break-even "
            f"{pct(self.record.break_even)} [{self.label}]"
        )


async def _closed(
    conn: AsyncConnection, m: mandates.Mandate
) -> list[tuple[object, Decimal, datetime, str | None]]:
    """This robot's closed learning trades (each opened in learning mode says so on its
    intent): (review, net, when, trade code)."""
    rows = await conn.execute(
        select(
            post_trade_reviews.c.id,
            post_trade_reviews.c.realized_pnl,
            post_trade_reviews.c.created_at,
            trade_intents.c.trade_code,
        )
        .select_from(post_trade_reviews)
        .join(trade_intents, trade_intents.c.id == post_trade_reviews.c.intent_id)
        .where(
            trade_intents.c.account_id == m.account_id,
            trade_intents.c.details["mandate_id"].astext == str(m.id),
            trade_intents.c.details["learning"].astext == "true",
            post_trade_reviews.c.realized_pnl.is_not(None),
        )
        .order_by(post_trade_reviews.c.created_at)
    )
    return [(r.id, Decimal(str(r.realized_pnl)), r.created_at, r.trade_code) for r in rows]


def _card(record: Record, nets: list[Decimal]) -> Card:
    return Card(
        record,
        len(nets),
        sum(1 for n in nets if n > 0),
        sum(1 for n in nets if n < 0),
        sum(nets, ZERO),
    )


async def card(conn: AsyncConnection, m: mandates.Mandate) -> Card | None:
    """The robot's learning card, or None for a rule learning mode does not cover."""
    record = RECORDS.get(m.strategy_key or "")
    if record is None:
        return None
    return _card(record, [net for _, net, _, _ in await _closed(conn, m)])


async def tell_closed(conn: AsyncConnection, m: mandates.Mandate, now: datetime) -> int:
    """Each closed demo trade, told once to the owner with the robot's card after it."""
    record = RECORDS.get(m.strategy_key or "")
    if record is None:
        return 0
    closed = await _closed(conn, m)
    told = 0
    for i, (review, net, at, code) in enumerate(closed):
        if now - at > TOLD_WITHIN:
            continue
        after = _card(record, [n for _, n, _, _ in closed[: i + 1]])
        word = "WON" if net > 0 else "LOST" if net < 0 else "EVEN"
        told += await notify_owners(
            conn,
            kind="gold_sim",
            payload={
                "text": f"{record.robot} (demo, learning mode): trade {code or ''} closed, "
                f"{word} {net:+,.2f} USD. {after.line()}."
            },
            now=now,
            dedup_key=f"learning:{review}",
        )
    return told
