"""Is a rule in use doing worse than its own history? (S48)

Each day, every daily rule an active auto-trader runs on gold has its forward record since the
rules were registered (S47, ``edge.forward_records``) set against its history before then. When
the record is rarer than 1 run in 20 of as many trades drawn from that history (``edge.UNUSUAL``),
the owner is told once, with the numbers; the road to real money shows it until the owner
acknowledges it. A flagged rule stays watched while its record is below 1 in 10 (the alert keeps
the latest numbers, without telling again), and the alert ends once the record is usual again,
whether or not an auto-trader still runs the rule: a rule may come back for real money later.

When a real-money auto-trader runs the rule, the alert says so and names that account; a
real-money account that starts running a flagged rule is told once on its own.

A rule doing worse than its history may have stopped working, or may be in a rare bad patch: a
daily rule trades about twice a month, so its record is short; the resampling draws trades one
by one, so losses that cluster look rarer than they are; and with several rules checked every
day, an alert now and then is chance. Nothing changes by itself: each auto-trader keeps its own
limits and its floor, and every real-money trade still waits for the owner's code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.clock import Clock
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.notify.alerts import Alert, notify_alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.reporting import autotrader, edge, rehearsal
from cosmic.trading import mandates
from cosmic.trading import rehearsal as rehearsals
from cosmic.trading.accounts import get_account
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, account_ref

PREFIX = "rule.unusual:"
CALM = 0.10  # an alert ends once the record is this usual again


@dataclass(frozen=True)
class User:
    """An account whose active auto-trader runs the rule."""

    ref: str  # with its environment, as alerts name accounts
    real: bool  # real money


async def rules_in_use(conn: AsyncConnection, now: datetime) -> dict[str, list[User]]:
    """Each daily rule an active auto-trader runs on gold (the records are replayed on gold),
    with the accounts running it."""
    out: dict[str, list[User]] = {}
    for m in await mandates.active_mandates(conn, now):
        on_gold = any(
            mandates.AUTO_SIGNAL_SERIES.get(k) == rehearsals.MARKET for k in m.instruments
        )
        if m.timeframe != mandates.DAILY or not on_gold:
            continue
        a = await get_account(conn, account_id=m.account_id)
        user = User(
            account_ref(a.alias, a.kind, a.broker_adapter), a.kind not in SIMULATED_ACCOUNT_KINDS
        )
        out.setdefault(m.strategy_key, []).append(user)
    return out


async def watched(conn: AsyncConnection) -> set[str]:
    """The rules with an alert not yet ended."""
    return {
        k.removeprefix(PREFIX)
        for (k,) in await conn.execute(
            select(incidents.c.dedup_key).where(
                incidents.c.dedup_key.startswith(PREFIX, autoescape=True),
                incidents.c.status != "RESOLVED",
            )
        )
    }


async def records(db: Database, now: datetime, rules: list[str]) -> list[edge.Forward] | None:
    """The forward records of ``rules`` on Binance gold, as the rehearsal page shows them (kept
    for the day). The prices are read inside a short connection and the replays run after it,
    off the event loop. None without prices."""
    if not rules:
        return []
    async with db.connect() as conn:
        b = await rehearsal.gold_bench(conn, now)
    if isinstance(b, autotrader.Expectation):
        return None
    return await asyncio.to_thread(edge.forward_records, b, rules, now)


def percent(share: float) -> str:
    """A share as a percent that never rounds up past a threshold (4.99, not 5.0)."""
    return f"{int(share * 10_000) / 100:.2f}"


CAVEAT = (
    "Whether the rule stopped working or this is a rare bad patch: a daily rule trades about "
    "twice a month, so its record is short; the resampling draws trades one by one, so losses "
    "that cluster look rarer than they are; and with several rules checked every day, an alert "
    "now and then is chance."
)


def _alert(f: edge.Forward, since: datetime, users: list[User], still: bool) -> Alert:
    assert f.as_bad is not None
    name = mandates.AUTO_RULES.get(f.key, f.key)
    real = [u for u in users if u.real]
    happened = (
        f"Since it was chosen ({since:%d %b %Y}): {f.trades} closed trade(s), {f.total:+.2f} at "
        f"$100 Bold (SIMULATED replay on Binance gold). Its history did this badly or worse in "
        f"only {percent(f.as_bad)} % of runs of as many trades."
        + (
            f" Used by: {', '.join(u.ref for u in users)}."
            if users
            else " No auto-trader runs it now."
        )
    )
    if real:
        risk = (
            f"Real money: {', '.join(u.ref for u in real)} runs this rule. It asks before each "
            "trade, so nothing goes out without your code."
        )
        owner = (
            "Read How far to trust it on Start with $100. Weigh each proposal from this rule, or "
            "end the real-money auto-trader on Go-live; then acknowledge this alert."
        )
        done = "Nothing changed: every real-money proposal still waits for your code."
    else:
        risk = (
            "None to money: only simulated auto-traders run it, and each keeps its own limits and "
            "stops for good at its floor."
        )
        owner = (
            "Read How far to trust it on Start with $100. Consider another rule at the next "
            "renewal, and do not start real money on this rule while this stands; then "
            "acknowledge this alert."
        )
        done = "Nothing changed: the auto-traders using it run on."
    return Alert(
        title=(
            f"The rule {name} is still doing worse than most of its history (watched)"
            if still
            else f"The rule {name} is doing worse than nearly all of its history"
        ),
        severity="WARNING",
        what_happened=happened,
        verified="Replayed on Binance gold with the same costs and limits as the rehearsals; its "
        "history is the trades before it was chosen, each counted against its own risk "
        "(HISTORICAL SIMULATION).",
        uncertain=CAVEAT,
        risk_now=risk,
        action_taken=done,
        owner_action=owner,
        account=real[0].ref if real else "",
    )


def _detail(f: edge.Forward, users: list[User]) -> dict[str, object]:
    """Persisted: decimals, never floats."""
    return {
        "rule": f.key,
        "trades": f.trades,
        "total": Decimal(f"{f.total:.2f}"),
        "as_bad": Decimal(f"{f.as_bad:.4f}") if f.as_bad is not None else None,
        "used_by": [u.ref for u in users],
        "real_money": [u.ref for u in users if u.real],
    }


async def tell(
    conn: AsyncConnection,
    now: datetime,
    found: list[edge.Forward],
    in_use: dict[str, list[User]],
    open_rules: set[str] | None = None,
) -> None:
    """Open, refresh or end each rule's alert from its record; tell a real-money account that
    starts running a flagged rule once."""
    since = mandates.RULES_REGISTERED[mandates.DAILY]
    if open_rules is None:
        open_rules = await watched(conn)
    for f in found:
        key, users = f"{PREFIX}{f.key}", in_use.get(f.key, [])
        if f.as_bad is None:
            continue  # no closed trade, or no history: nothing to judge
        if f.unusual or (f.key in open_rules and f.as_bad < CALM):
            alert = _alert(f, since, users, still=not f.unusual)
            await raise_incident(
                conn,
                dedup_key=key,
                category=Category.TRADING,
                alert=alert,
                now=now,
                detail=_detail(f, users),
            )
            for u in users:  # a real-money account running a flagged rule: told once
                if u.real:
                    await notify_alert(
                        conn, kind="incident", alert=alert, now=now, dedup_key=f"{key}:real:{u.ref}"
                    )
        elif f.key in open_rules:
            await resolve_by_key(
                conn, dedup_key=key, now=now, resolution="its record is usual again"
            )


async def watch(db: Database, clock: Clock) -> list[edge.Forward]:
    """The worker's ``rehearsal.rule_watch``: the rules in use and those still watched, their
    records replayed, then the alerts in their own short transaction."""
    now = clock.now()
    async with db.connect() as conn:
        in_use = await rules_in_use(conn, now)
        open_rules = await watched(conn)
    found = await records(db, now, sorted(set(in_use) | open_rules))
    if found is None:
        return []  # no prices yet: alerts stay as they are
    async with db.transaction() as conn:
        await tell(conn, clock.now(), found, in_use, open_rules)
    return found
