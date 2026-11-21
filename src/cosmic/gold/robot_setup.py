"""Robot A's own HFM demo sets itself up, and START when ready (S111; the owner's request of
2026-11-21: "make the manual things more automated and fast ... with your idea").

Every minute (``gold.robot_setup``), while Robot A's own demo account (terminal 2) is not open:

1. **Its terminal:** terminal 2 must be reporting, signed in to a DEMO account in US dollars that
   no other robot's account was opened on (each robot its own account, the owner's choice of
   2026-11-21). Otherwise it waits and tells the owner why, once a day.
2. **The market:** while gold's market is closed (no fresh HFM price) it waits: HFM refuses any
   order then ("Market closed"), the check's too.
3. **Its safety check** (the broker contract, ``hfm_check.run_contract``, as ``cosmic broker
   hfm-check --bridge hfm-demo-2`` runs it): one buy limit far below the price that cannot fill,
   with HFM holding its stop-loss, found again, cancelled, and the balance read; recorded as
   evidence. A failed check is tried again after ``RETRY``; the owner is told once a day for
   each reason.
4. **Its account:** with that check PASSED for this very MT5 account, the account opens
   (``ALIAS``, its money up to Robot A's 100 USD managed), through every gate of
   ``sandbox.open_sandbox_account``.

Then, **START when ready:** when the owner approved in advance, with their code, that Robot A
starts the moment its demo is ready (``arm``), it starts (``trend_test.start``, every check of a
grant included); the approval ends STARTED, or REFUSED with why; it lapses after ``LIFE``.
Without one, the owner is told the demo is ready for their START.

The owner is told on Telegram at each step. Nothing here trades: a trade comes only from
Robot A's own rule once it runs. Real accounts are never set up here (live trading has its own,
separate authorization).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from cosmic.core.ids import uuid7
from cosmic.core.logs import get_logger
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_gold import robot_starts
from cosmic.db.schema_trading import accounts, bridge_state
from cosmic.gold import sim, trend_test
from cosmic.identity.service import Principal
from cosmic.notify.outbox import notify_owners
from cosmic.trading import connections
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers.base import BrokerUnavailableError
from cosmic.trading.brokers.hfm_check import CONTRACT_VERSION, run_contract
from cosmic.trading.brokers.mt5_bridge import (
    ADAPTER_2,
    BRIDGE_2,
    QUOTE,
    HfmBridgeAdapter,
    hfm_symbols,
)
from cosmic.trading.sandbox import (
    latest_contract_run,
    mt5_account_in_use,
    open_sandbox_account,
    record_contract_run,
)

log = get_logger(__name__)

ROBOT = "ROBOT_A"
ALIAS = "hfm-demo-2"  # Robot A's own demo account, as it opens
ACTOR = Actor.system("robot_setup")
LIFE = timedelta(days=3)  # a START when ready lapses after this
RETRY = timedelta(minutes=15)  # a failed safety check is tried again after this
STARTED, REFUSED, CANCELLED, EXPIRED = "STARTED", "REFUSED", "CANCELLED", "EXPIRED"
_running = asyncio.Lock()  # one setup step at a time in the worker (a check takes seconds)


@dataclass(frozen=True)
class Setup:
    """Where Robot A's own demo is in its setup, for its panel."""

    stage: str  # WAITING, CHECKING, READY, RUNNING
    text: str
    armed: Any | None = None  # the owner's START when ready, while it waits


async def _tell(conn: AsyncConnection, key: str, text: str, now: datetime) -> None:
    await notify_owners(conn, kind="gold_sim", payload={"text": text}, now=now, dedup_key=key)


async def own_account(conn: AsyncConnection) -> uuid.UUID | None:
    """Robot A's own demo account (terminal 2's), once open."""
    return await conn.scalar(
        select(accounts.c.id).where(
            accounts.c.broker_adapter == ADAPTER_2,
            accounts.c.kind == "SANDBOX",
            accounts.c.status == "ACTIVE",
        )
    )


async def waiting(conn: AsyncConnection) -> Any | None:
    """The owner's START when ready that has not ended yet (it may be past its time: the next
    setup step ends it)."""
    return (
        await conn.execute(
            select(robot_starts).where(
                robot_starts.c.robot == ROBOT, robot_starts.c.ended.is_(None)
            )
        )
    ).first()


async def _end(
    conn: AsyncConnection,
    row_id: uuid.UUID,
    how: str,
    now: datetime,
    *,
    actor: Actor = ACTOR,
    mandate_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    await conn.execute(
        update(robot_starts)
        .where(robot_starts.c.id == row_id, robot_starts.c.ended.is_(None))
        .values(ended=how, ended_at=now, mandate_id=mandate_id, reason=(reason or None))
    )
    await record(
        conn,
        action=f"robot.start_in_advance_{how.lower()}",
        actor=actor,
        occurred_at=now,
        entity_type="robot_start",
        entity_id=row_id,
        payload={"mandate_id": str(mandate_id) if mandate_id else None, "reason": reason},
    )


async def arm(conn: AsyncConnection, *, principal: Principal, now: datetime) -> uuid.UUID:
    """The owner's START when ready (their code is checked by the caller): Robot A starts on its
    own demo the moment that demo is ready, with the terms kept here, once."""
    if await trend_test.test_mandates(conn, now):
        raise ConflictError("Robot A is running already")
    if await waiting(conn) is not None:
        raise ConflictError("your START when ready is already waiting")
    terms = trend_test.terms_for(ALIAS)
    start_id = uuid7()
    shown = {
        "account": ALIAS,
        "amount": str(terms.capital),
        "max_loss_per_trade": str(terms.max_loss_per_trade),
        "daily_loss_limit": str(terms.daily_loss_limit),
        "total_loss_limit": str(terms.total_limit),
        "drawdown_pct": str(trend_test.DRAWDOWN_PCT),
        "ounces": "1",
    }
    await conn.execute(
        robot_starts.insert().values(
            id=start_id,
            robot=ROBOT,
            owner_id=principal.user_id,
            armed_at=now,
            armed_by=f"{principal.actor.type.value}:{principal.actor.id}",
            expires_at=now + LIFE,
            terms=shown,
        )
    )
    await record(
        conn,
        action="robot.start_approved_in_advance",
        actor=principal.actor,
        occurred_at=now,
        entity_type="robot_start",
        entity_id=start_id,
        payload={**shown, "expires_at": (now + LIFE).isoformat()},
    )
    await _tell(
        conn,
        f"robot-start:{start_id}:armed",
        "ROBOT A — START WHEN READY approved with your code: Robot A starts by itself on its own "
        "HFM demo the moment it is ready (its safety check passed and its account opened, as "
        "soon as gold's market is open), with 1 ounce a trade, at most "
        f"{terms.max_loss_per_trade:,.2f} USD at risk, {terms.daily_loss_limit:,.2f} a day and "
        f"a stop after {terms.total_limit:,.2f} lost. It lapses on {now + LIFE:%d %b %H:%M} UTC; "
        "you can cancel it on the gold page.",
        now,
    )
    return start_id


async def cancel(conn: AsyncConnection, *, actor: Actor, now: datetime, why: str) -> bool:
    """The owner's START when ready ends without starting: taken back, or Robot A started by
    the owner's own START. True when one was waiting."""
    row = await waiting(conn)
    if row is None:
        return False
    await _end(conn, row.id, CANCELLED, now, actor=actor, reason=why)
    return True


async def _terminal(conn: AsyncConnection, now: datetime) -> tuple[dict[str, Any] | None, str]:
    """Terminal 2's account as its bridge reports it now, or None and why not."""
    row = (
        await conn.execute(
            select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                bridge_state.c.bridge == BRIDGE_2
            )
        )
    ).first()
    if row is None or now - row.received_at > connections.FRESH:
        return None, (
            "terminal 2 (Robot A's own MT5 on the Windows computer) is not reporting; it "
            "starts by itself with the computer"
        )
    account = dict((row.snapshot or {}).get("account") or {})
    if not account:
        return None, (
            "terminal 2 is signed in to no account: sign it in to Robot A's own demo "
            "(File → Login to Trade Account)"
        )
    if account.get("trade_mode") != "DEMO" or account.get("currency") != QUOTE:
        return None, "terminal 2 is not signed in to a demo account in US dollars"
    other = await mt5_account_in_use(conn, str(account.get("fingerprint") or ""), ADAPTER_2)
    if other is not None:
        return None, (
            f"terminal 2 is signed in to {other}'s account (Robot B's): each robot has its own "
            "account, so sign terminal 2 in to Robot A's own demo"
        )
    return account, ""


async def status(conn: AsyncConnection, now: datetime) -> Setup:
    """For the panel: where Robot A's own demo is in its setup."""
    armed = await waiting(conn)
    if await trend_test.test_mandates(conn, now):
        return Setup("RUNNING", "Robot A is running on its own HFM demo")
    open_alias = await conn.scalar(
        select(accounts.c.alias).where(
            accounts.c.broker_adapter == ADAPTER_2,
            accounts.c.kind == "SANDBOX",
            accounts.c.status == "ACTIVE",
        )
    )
    if open_alias is not None:
        return Setup("READY", f"its own demo account ({open_alias}) is open", armed)
    account, why = await _terminal(conn, now)
    if account is None:
        return Setup("WAITING", why, armed)
    run = await latest_contract_run(conn, adapter=ADAPTER_2)
    if await sim.price_now(conn, now) is None:
        return Setup(
            "WAITING",
            "terminal 2 is signed in to Robot A's own demo; gold's market is closed, so its "
            "safety check runs by itself when it opens",
            armed,
        )
    if (
        run is not None
        and run.status == "FAILED"
        and run.key_fingerprint == account.get("fingerprint")
    ):
        failed = next((s["name"] for s in run.steps if not s["passed"]), "a step")
        return Setup(
            "CHECKING",
            f"its safety check failed at {run.finished_at:%H:%M} UTC ({failed}); it tries "
            f"again at {run.finished_at + RETRY:%H:%M} UTC",
            armed,
        )
    return Setup("CHECKING", "its safety check runs now, by itself", armed)


async def tick(
    db: Database, clock: Clock, settings: Settings, adapter: HfmBridgeAdapter | None = None
) -> str | None:
    """One setup step (the worker, every minute); what it did, or None. ``adapter``: terminal
    2's, as the worker's trading services hold it (made here when not given)."""
    if _running.locked():
        return None
    async with _running:
        return await _tick(db, clock, settings, adapter)


async def _tick(
    db: Database, clock: Clock, settings: Settings, given: HfmBridgeAdapter | None
) -> str | None:
    now = clock.now()
    async with db.transaction() as conn:
        armed = await waiting(conn)
        if armed is not None and now >= armed.expires_at:
            await _end(conn, armed.id, EXPIRED, now, reason="its demo was not ready in time")
            await _tell(
                conn,
                f"robot-start:{armed.id}:expired",
                "ROBOT A — your START WHEN READY lapsed after 3 days: its own demo was not "
                "ready. Approve it again on the gold page when you want.",
                now,
            )
            armed = None
        if await trend_test.test_mandates(conn, now):
            if armed is not None:
                await _end(conn, armed.id, CANCELLED, now, reason="Robot A was running already")
            return None
        opened = await own_account(conn)
        if opened is not None:
            return await _start(conn, opened, armed, now, settings)
        terminal, why = await _terminal(conn, now)
        if terminal is None:
            await _tell(
                conn, f"robot-setup:{why[:60]}:{now:%Y-%m-%d}", f"ROBOT A SETUP — {why}.", now
            )
            return "waiting"
        fingerprint = str(terminal.get("fingerprint") or "")
        if await sim.price_now(conn, now) is None:
            return "market closed"
        run = await latest_contract_run(conn, adapter=ADAPTER_2)
        passed = run is not None and run.status == "PASSED" and run.key_fingerprint == fingerprint
        if (
            not passed
            and run is not None
            and run.key_fingerprint == fingerprint
            and (now - run.finished_at < RETRY)
        ):
            return "check failed; waiting to try again"
    adapter = given or HfmBridgeAdapter(db, clock, hfm_symbols(), bridge=BRIDGE_2)
    if not passed:
        started = clock.now()
        steps = await run_contract(adapter)
        finished = clock.now()
        async with db.transaction() as conn:
            _, passed = await record_contract_run(
                conn,
                steps=steps,
                key_fingerprint=fingerprint,
                code_version=CONTRACT_VERSION,
                started_at=started,
                finished_at=finished,
                actor=ACTOR,
                adapter=ADAPTER_2,
            )
            if not passed:
                failed = next((s for s in steps if not s.passed), None)
                what = (
                    f"{failed.name}" + (f" ({failed.detail[:120]})" if failed.detail else "")
                    if failed is not None
                    else "no step ran"
                )
                await _tell(
                    conn,
                    f"robot-setup:check-failed:{failed.name if failed else ''}:{now:%Y-%m-%d}",
                    f"ROBOT A SETUP — terminal 2's safety check failed: {what}. Nothing was "
                    f"traded. It tries again by itself every {RETRY.seconds // 60} minutes while "
                    "the market is open.",
                    finished,
                )
                return "check failed"
            await _tell(
                conn,
                f"robot-setup:check-passed:{fingerprint}",
                "ROBOT A SETUP — terminal 2's safety check PASSED on Robot A's own demo: a test "
                "order far below the price (it could not fill) was placed with its stop-loss "
                "held by HFM, found and cancelled. Its account opens now.",
                finished,
            )
    try:
        balances = await adapter.get_balances(uuid.UUID(int=0))
    except BrokerUnavailableError as exc:
        log.info("robot_setup_balances_unavailable", reason=str(exc)[:200])
        return "balances unavailable"
    free = next((b.available for b in balances if b.asset == QUOTE), ZERO)
    managed = min(free, trend_test.AMOUNT)
    now = clock.now()
    async with db.transaction() as conn:
        if managed <= ZERO:
            await _tell(
                conn,
                f"robot-setup:no-money:{now:%Y-%m-%d}",
                "ROBOT A SETUP — Robot A's own demo holds no free US dollars: top it up to 100 "
                "USD in myHF.",
                now,
            )
            return "no money"
        try:
            async with conn.begin_nested():
                account_id = await open_sandbox_account(
                    conn,
                    alias=ALIAS,
                    managed_capital=managed,
                    balances=balances,
                    key_fingerprint=fingerprint,
                    now=now,
                    actor=ACTOR,
                    adapter=ADAPTER_2,
                )
        except (ConflictError, NotFoundError, ValidationFailedError) as exc:
            await _tell(
                conn,
                f"robot-setup:open-refused:{now:%Y-%m-%d}",
                f"ROBOT A SETUP — its demo account did not open: {exc}.",
                now,
            )
            return "open refused"
        armed = await waiting(conn)
        await _tell(
            conn,
            f"robot-setup:opened:{account_id}",
            f"ROBOT A SETUP — Robot A's own demo account is open ({ALIAS}, {managed:,.2f} USD "
            "managed). "
            + (
                "Robot A starts now: you approved it in advance."
                if armed is not None
                else "Press START Robot A on the gold page, with your code, when you want it to "
                "trade."
            ),
            now,
        )
        return await _start(conn, account_id, armed, now, settings) or "account opened"


async def _start(
    conn: AsyncConnection,
    account_id: uuid.UUID,
    armed: Any | None,
    now: datetime,
    settings: Settings,
) -> str | None:
    """Robot A starts from the owner's approval in advance, when there is one."""
    if armed is None:
        return None
    account = await get_account(conn, account_id=account_id)
    try:
        async with conn.begin_nested():
            mandate_id = await trend_test.start(
                conn,
                account=account,
                now=now,
                settings=settings,
                owner=armed.owner_id,
                actor=ACTOR,
            )
    except (ConflictError, ValidationFailedError, PermissionDeniedError) as exc:
        await _end(conn, armed.id, REFUSED, now, reason=str(exc)[:300])
        await _tell(
            conn,
            f"robot-start:{armed.id}:refused",
            f"ROBOT A did not start: {exc}. Your START WHEN READY ended; nothing was traded.",
            now,
        )
        return "start refused"
    await _end(conn, armed.id, STARTED, now, mandate_id=mandate_id)
    m_terms = armed.terms or {}
    await _tell(
        conn,
        f"robot-start:{armed.id}:started",
        f"ROBOT A STARTED on its own HFM demo ({account.alias}), as you approved in advance on "
        f"{armed.armed_at:%d %b %H:%M} UTC: 1 ounce a trade, BUY or SELL at its signal, at most "
        f"{m_terms.get('max_loss_per_trade', '30')} USD at risk a trade, a stop after "
        f"{m_terms.get('total_loss_limit', '60')} USD lost. Its simulation is off.",
        now,
    )
    return "started"
