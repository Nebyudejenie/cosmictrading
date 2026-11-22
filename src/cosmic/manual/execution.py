"""Portal execution of a manual trade on the real account (S113): built, and LOCKED.

The owner's decision of 2026-11-22: they place their trades in MT5 now; the portal's own
BUY/SELL stays locked until the nine go-live gates pass and they activate live trading. Every
lock is checked here, each named in plain words, and while any is closed a submission is
refused (audited) before anything is created or sent:

1. an HFM live broker adapter registered in ``LIVE_ADAPTERS`` (a reviewed code change, with its
   sandbox contract run: none exists in this build);
2. the server's ceiling (``COSMIC_MAX_AUTHORITY_MODE``) and its live venues
   (``COSMIC_LIVE_VENUES``), in the server's own env file;
3. the system's authority mode: APPROVAL_REQUIRED, every trade approved by the owner;
4. the nine go-live gates, as ``golive.readiness`` evaluates them;
5. the owner's live activation for the account, with its money cap and expiry;
6. the real account itself: linked, verified REAL, reporting, and signed in with a password
   HFM accepts orders on (the read-only investor sign-in refuses every order).

Nothing here opens a lock. "Trade opened" is never said for a request: only HFM's own
acknowledgement, order and position, reconciled with the account, could say it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.audit.service import record as audit
from cosmic.core.config import Settings
from cosmic.trading import golive
from cosmic.trading.authority import LIVE_ADAPTERS, get_state, mode_cap
from cosmic.trading.live import active_activations
from cosmic.trading.modes import LIVE_MODES, Mode

HFM_LIVE_ADAPTER = "hfm_mt5_live"  # the name it will have; not registered in this build
VENUE = "HFM"


async def locks(
    conn: AsyncConnection,
    settings: Settings,
    now: datetime,
    *,
    account_live: bool,
    account_read_only: bool,
) -> list[str]:
    """Every lock still closed, in plain words (empty: none; never in this build)."""
    closed: list[str] = []
    if HFM_LIVE_ADAPTER not in LIVE_ADAPTERS:
        closed.append(
            "the HFM live connection is not built into this version (lock 1: it is added only "
            "as its own reviewed change, with its contract tests)"
        )
    cap = mode_cap(settings)
    if cap not in LIVE_MODES:
        closed.append(f"the server allows at most {cap.value} (its own settings file)")
    if VENUE not in settings.live_venues:
        closed.append("the server lists no live venue for HFM (its own settings file)")
    state = await get_state(conn)
    if state.mode != Mode.APPROVAL_REQUIRED:
        closed.append(
            f"the system's authority is {state.mode.value}; real trades need APPROVAL_REQUIRED "
            "(you approve every trade)"
        )
    gates = await golive.readiness(conn, now)
    waiting = [g.key for g in gates if g.status != "PASSED"]
    if waiting:
        closed.append(
            f"{len(gates) - len(waiting)} of {len(gates)} safety gates passed (still to pass: "
            f"{', '.join(waiting)})"
        )
    if not await active_activations(conn, now):
        closed.append(
            "you have not activated live trading (with your code, a money cap and an end date)"
        )
    if not account_live:
        closed.append("your live account is not connected and verified")
    elif account_read_only:
        closed.append(
            "your live account is signed in read-only (its investor password): HFM refuses "
            "orders on it, as intended for now"
        )
    return closed


async def submit(
    conn: AsyncConnection,
    settings: Settings,
    now: datetime,
    *,
    actor: Actor,
    summary: dict[str, object],
    account_live: bool,
    account_read_only: bool,
) -> list[str]:
    """The portal's BUY/SELL: refused while any lock is closed (always, in this build). The
    refusal is recorded in the audit log and its reasons returned; nothing is created or sent."""
    closed = await locks(
        conn, settings, now, account_live=account_live, account_read_only=account_read_only
    ) or ["no HFM live adapter in this build"]
    await audit(
        conn,
        action="manual.portal_execution_refused",
        actor=actor,
        occurred_at=now,
        entity_type="manual_trade",
        entity_id=None,
        payload={"order": summary, "locks": closed},
    )
    return closed
