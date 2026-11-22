"""The owner's closed trades on the real account, from HFM's own deals (S113), and the
dashboard's figures from them. Read only; nothing here is estimated or simulated."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from cosmic.manual.live import ENTRY_IN, ENTRY_INOUT, ENTRY_OUT, ENTRY_OUT_BY, Deal

ZERO = Decimal(0)


@dataclass(frozen=True)
class Trade:
    """One position from its opening deal to its last closing deal."""

    position_id: int
    side: str
    lots: Decimal
    opened_at: datetime
    closed_at: datetime
    closed_server_at: datetime
    entry: Decimal
    exit: Decimal
    net: Decimal  # profit, swap, commission and fees of every deal of the position
    magic: int

    @property
    def won(self) -> bool:
        return self.net > 0


def closed_trades(deals: Sequence[Deal]) -> list[Trade]:
    """Positions whose closing volume reached their opening volume, oldest close first."""
    by: dict[int, list[Deal]] = {}
    for d in deals:
        if d.side is not None and d.position_id:
            by.setdefault(d.position_id, []).append(d)
    out = []
    for pid, ds in by.items():
        ins = [d for d in ds if d.entry in (ENTRY_IN, ENTRY_INOUT)]
        outs = [d for d in ds if d.entry in (ENTRY_OUT, ENTRY_OUT_BY, ENTRY_INOUT)]
        if not ins or not outs:
            continue
        opened = sum((d.lots for d in ins), ZERO)
        closed = sum((d.lots for d in outs), ZERO)
        if closed < opened:
            continue
        first, last = ins[0], outs[-1]
        out.append(
            Trade(
                position_id=pid,
                side=first.side or "",
                lots=opened,
                opened_at=first.at,
                closed_at=last.at,
                closed_server_at=last.server_at,
                entry=first.price,
                exit=last.price,
                net=sum((d.net for d in ds), ZERO),
                magic=first.magic,
            )
        )
    return sorted(out, key=lambda t: (t.closed_at, t.position_id))


def opened_since(deals: Sequence[Deal], start: datetime) -> int:
    """Trades opened since ``start`` (opening deals)."""
    return sum(1 for d in deals if d.side is not None and d.entry == ENTRY_IN and d.at >= start)


def realised_since(deals: Sequence[Deal], start: datetime) -> Decimal:
    """Money made or lost since ``start`` by trading deals (closing deals and their costs)."""
    return sum((d.net for d in deals if d.side is not None and d.at >= start), ZERO)


def losses_in_a_row(trades: Sequence[Trade]) -> int:
    n = 0
    for t in reversed(trades):
        if t.net < 0:
            n += 1
        else:
            break
    return n


@dataclass(frozen=True)
class Stats:
    trades: int
    wins: int
    win_rate: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    profit_factor: Decimal | None
    expectancy: Decimal | None  # per trade
    net: Decimal
    max_drawdown: Decimal  # the deepest fall of the closed-trade result from its best


def stats(trades: Sequence[Trade]) -> Stats:
    wins = [t.net for t in trades if t.net > 0]
    losses = [t.net for t in trades if t.net < 0]
    n = len(trades)
    gross_win, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
    run = best = drawdown = ZERO
    for t in trades:
        run += t.net
        best = max(best, run)
        drawdown = max(drawdown, best - run)
    return Stats(
        trades=n,
        wins=len(wins),
        win_rate=Decimal(len(wins)) / n * 100 if n else None,
        avg_win=gross_win / len(wins) if wins else None,
        avg_loss=-gross_loss / len(losses) if losses else None,
        profit_factor=gross_win / gross_loss if gross_loss > 0 else None,
        expectancy=(gross_win - gross_loss) / n if n else None,
        net=gross_win - gross_loss,
        max_drawdown=drawdown,
    )


def period(trades: Sequence[Trade], start: datetime) -> Decimal:
    return sum((t.net for t in trades if t.closed_at >= start), ZERO)


def week_start(day_start: datetime, server_offset: timedelta | None) -> datetime:
    """Monday of the broker's week containing ``day_start`` (a broker day's start, UTC)."""
    local = day_start + (server_offset or timedelta(0))
    return day_start - timedelta(days=local.weekday())


def month_start(day_start: datetime, server_offset: timedelta | None) -> datetime:
    local = day_start + (server_offset or timedelta(0))
    return day_start - timedelta(days=local.day - 1)
