"""Is each rehearsal doing what its own replay says? (S45)

The rehearsal page compares each started rehearsal with its replay over the same days, trade by
trade (S39), but nobody was told when they part. This watch, every six hours, tells the owner:

- **the first time a run parts** from its replay (a trade the replay made and the rehearsal did
  not, one the rehearsal made and the replay did not, or a trade that closed another way): one
  WARNING alert for that run (its mandate), naming the days;
- **each later parting day once**, as a notice on that alert.

The alert stays until the owner acknowledges it or the run ends or is renewed. It never ends and
reopens by itself: a replay of past days is worked out again each time (the typical spread, the
policy now), so a borderline day may come and go, and the owner is never told twice about it.

Gate G's evidence assumes the system does what its simulation says: a parting is a fault, a
limit or a data gap to explain before real money. It changes nothing by itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.clock import Clock
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.notify.alerts import Alert, notify_alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.reporting import rehearsal
from cosmic.trading.modes import account_ref

PREFIX = "rehearsal.off_replay:"


@dataclass(frozen=True)
class Run:
    """A started rehearsal's run (its mandate) compared with its replay."""

    alias: str
    kind: str
    name: str
    mandate_id: uuid.UUID
    ended: bool  # the run has ended (stopped, expired or revoked): nothing more to tell
    days: tuple[date, ...]  # the days it parts from its replay, oldest first
    verdict: str

    @property
    def key(self) -> str:
        return f"{PREFIX}{self.alias}:{self.mandate_id}"


async def runs(conn: AsyncConnection, now: datetime) -> list[Run]:
    """Every rehearsal compared with its replay now. One whose replay could not run is left out:
    it is neither parted nor following."""
    out: list[Run] = []
    for st in await rehearsal.trackings(conn, now):
        k = st.tracking
        if st.account is None or k is None or k.mandate_id is None or k.replay.summary is None:
            continue
        days = tuple(sorted(p.day for p in k.pairs if p.differs))
        out.append(
            Run(
                st.account.alias,
                st.account.kind,
                st.venue.name,
                k.mandate_id,
                k.until < now,
                days,
                k.verdict,
            )
        )
    return out


def _alert(r: Run, new: list[date] | None = None) -> Alert:
    if new:
        title = f"The $100 rehearsal on {r.name} parted from its replay again"
    else:
        title = f"The $100 rehearsal on {r.name} did not follow its replay"
    return Alert(
        title=title,
        severity="WARNING",
        what_happened=r.verdict,
        verified="Its trades compared day by day with the replay of the same days, on the same "
        "prices, rule and limits (SIMULATED; Start with $100 → Your rehearsals).",
        uncertain="Why: a skipped signal (no fresh price, a risk limit, the fee), a fill that "
        "landed differently, a price the venue's own quotes did not reach, or a fault.",
        risk_now="None to money: the rehearsal is simulated. But gate G's evidence assumes the "
        "system does what its simulation says.",
        action_taken="Nothing changed by this alert: the rehearsal runs on.",
        owner_action="Look at its skipped signals on Start with $100 and the auto-trader page, "
        "then acknowledge this alert; a fault is fixed before real money. It ends by itself "
        "when the run ends or is renewed.",
        account=account_ref(r.alias, r.kind),
    )


async def _open(conn: AsyncConnection) -> dict[str, dict[str, Any]]:
    """The watch's alerts not resolved (acknowledged ones included), by key, with their
    detail."""
    rows = await conn.execute(
        select(incidents.c.dedup_key, incidents.c.detail).where(
            incidents.c.dedup_key.startswith(PREFIX, autoescape=True),
            incidents.c.status != "RESOLVED",
        )
    )
    return {r.dedup_key: dict(r.detail or {}) for r in rows}


async def tell(conn: AsyncConnection, now: datetime, found: list[Run]) -> None:
    """Open an alert the first time a run parts, tell each later parting day once, and end
    the alerts of runs that ended or were renewed."""
    alive = {r.key for r in found if not r.ended}
    compared = {r.alias for r in found}
    for key in await _open(conn):
        alias = key.removeprefix(PREFIX).rsplit(":", 1)[0]
        if alias in compared and key not in alive:
            await resolve_by_key(
                conn,
                dedup_key=key,
                now=now,
                resolution="its run ended or was renewed: a new comparison starts",
            )
    told_now = await _open(conn)
    for r in found:
        if r.ended or not r.days:
            continue  # nothing new to tell; an alert already open stays for the owner
        days = [d.isoformat() for d in r.days]
        told = list(told_now.get(r.key, {}).get("told") or [])
        new = [d for d in r.days if d.isoformat() not in told]
        if r.key in told_now and not new:
            continue
        _, opened = await raise_incident(
            conn,
            dedup_key=r.key,
            category=Category.TRADING,
            alert=_alert(r),
            now=now,
            detail={"days": days, "told": sorted({*told, *days})},
        )
        if not opened:  # a later day: told once, on its own
            await notify_alert(
                conn,
                kind="incident",
                alert=_alert(r, new),
                now=now,
                dedup_key=f"{r.key}:{new[-1].isoformat()}",
            )


async def watch(db: Database, clock: Clock) -> list[Run]:
    """The worker's ``rehearsal.replay_watch``. The replays are worked out first; the alerts
    are written after, in their own short transaction."""
    async with db.connect() as conn:
        found = await runs(conn, clock.now())
    async with db.transaction() as conn:
        await tell(conn, clock.now(), found)
    return found
