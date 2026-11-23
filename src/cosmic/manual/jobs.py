"""Manual Trading's worker job (S117): a setup the page recommends (TAKE or YOUR CALL) goes to
the owner's Telegram and portal at once, once a 15-minute candle, with the trade worked out:
side, lots, stop-loss, take-profit, money at risk and the record behind it.

Only for the verified real account (never a preview), and only when the owner's own rules allow
the trade now. It reads; it never sends an order. No account number or secret is in the message.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.logs import get_logger
from cosmic.db.engine import Database
from cosmic.manual import advice, page
from cosmic.market import hfm_candles
from cosmic.notify.outbox import notify_owners
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_DATA
from cosmic.worker.registry import Periodic, Registry

log = get_logger(__name__)
EVERY_S = 15
KIND = "manual_setup"
SEEN = "manual.setup_watch.candle"  # the newest candle already judged (this worker)
CANDLE = timedelta(minutes=15)
AFTER_CLOSE = timedelta(seconds=20)  # after a close, the robots' moment, before building


def message(w: page.Workspace) -> str | None:
    """The alert for the next trade the page works out, or None when it is not one to send."""
    t, s, a = w.auto_ticket, w.auto_sizing, w.advice
    if not w.account.live or t is None or s is None or s.blocked or w.idea.as_of is None:
        return None
    if w.base is None:
        return None
    if a.level not in (advice.TAKE, advice.YOUR_CALL) or w.auto_verdict == "BLOCK":
        return None
    symbol = w.account.symbol or "XAUUSD"
    cur = w.account.currency or ""
    until: datetime = w.idea.as_of + 2 * CANDLE  # the candle read closed; the next one closes
    rr = w.plan.rr.normalize()
    record = (
        f"{a.p * 100:.1f} % of {a.n} such trades reached the target first "
        f"(95 % range {a.low * 100:.1f}–{a.high * 100:.1f} %); it needs "
        f"{a.breakeven * 100:.1f} % to break even after the spread"
        if a.p is not None and a.low is not None and a.high is not None and a.breakeven
        else a.why
    )
    label = "verified, out of sample" if a.verified else "in sample, NOT verified"
    reward = f" · at the target {s.reward_cash:+.2f}" if s.reward_cash is not None else ""
    return "\n".join(
        [
            f"GOLD SETUP — {a.level} ({w.account.broker or 'your broker'}, your real account)",
            f"{t.side} {s.lots.normalize():f} lot {symbol} at market "
            f"({'ask' if t.side == 'BUY' else 'bid'} {t.entry:.2f})",
            f"Stop-loss {t.stop:.2f} · Take-profit {t.target:.2f} (1:{rr:f})"
            if t.target is not None
            else f"Stop-loss {t.stop:.2f}",
            f"At the stop {-s.risk_cash:.2f} {cur} ({s.risk_pct:.1f} % of "
            f"{w.base:.2f}){reward}".rstrip(),
            f"Record: {record} — {label}.",
            f"Valid until {until:%H:%M} UTC (the next 15-minute close). You place it in "
            f"{w.account.broker or 'MT5'} yourself; nothing was sent.",
        ]
    )


async def newest_candle(db: Database) -> datetime | None:
    """The newest complete 15-minute candle: a setup can only change when it does."""
    async with db.connect() as conn:
        last = await hfm_candles.candles(conn, "15m", 1)
    return last[-1].open_time if last else None


async def setup_watch(db: Database, clock: Clock, settings: Settings) -> str | None:
    """One look: a recommended setup is told once per candle and side (the dedup key)."""
    now = clock.now()
    async with db.connect() as conn:
        w = await page.build(conn, now, settings, {}, light=True)
    text = message(w)
    if text is None or w.auto_ticket is None or w.idea.as_of is None:
        return None
    key = f"manual.setup:{w.idea.as_of:%Y%m%dT%H%M}:{w.auto_ticket.side}"
    async with db.transaction() as conn:
        told = await notify_owners(
            conn,
            kind=KIND,
            payload={"text": text, "side": w.auto_ticket.side, "level": w.advice.level},
            now=now,
            dedup_key=key,
        )
    return text if told else None


def register(registry: Registry, ctx: WorkerContext) -> None:
    @registry.handler("manual.setup_watch")
    async def watch(ctx: WorkerContext, payload: dict[str, Any]) -> None:
        """Every 15 s a cheap look at the newest 15-minute candle; the page is built (about
        0.2 s of CPU) only once after each new one, so the robots in this worker are never
        slowed by it."""
        try:
            candle = await newest_candle(ctx.db)
            if candle is None or ctx.services.get(SEEN) == candle:
                return
            if ctx.clock.now() < candle + CANDLE + AFTER_CLOSE:
                return  # the robots decide right at a close: this waits until they have
            told = await setup_watch(ctx.db, ctx.clock, ctx.settings)
        except Exception as exc:  # a missed look; the next one comes in seconds
            log.error("manual_setup_watch_failed", error=type(exc).__name__, detail=str(exc)[:200])
            return
        ctx.services[SEEN] = candle  # judged: not again until the next candle
        if told:
            log.info("manual_setup_told")

    registry.every(Periodic("manual.setup_watch", EVERY_S, PRIORITY_DATA))
