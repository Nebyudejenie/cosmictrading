"""Reconciliation (V6 §1818): BROKER_ORDERS → FILLS → POSITIONS → BALANCES → LEDGER →
ACCOUNT_STATE. Broker-confirmed facts are imported (audited); anything unexplained is a
MISMATCH that fails the run, marks the account FAILED and blocks new exposure. Internal
records are never overwritten with guesses."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.clock import Clock
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_execution import fills, reconciliation_items, reconciliation_runs
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_market import instruments
from cosmic.db.schema_trading import positions
from cosmic.market import data as md
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.trading import ledger
from cosmic.trading.accounts import get_account, set_reconciliation_status
from cosmic.trading.brokers.base import BrokerAmbiguousError, BrokerUnavailableError
from cosmic.trading.brokers.registry import BrokerRegistry
from cosmic.trading.execution import ACTIVE_STATES, ExecutionService
from cosmic.trading.orders import OrderStatus, Source, list_orders
from cosmic.trading.sandbox import tracked_assets

ACTOR = Actor.system("reconciliation")
# Venue and server clocks differ slightly; a trade this close before opening is still counted
# (and then fails as unknown, which is the safe direction).
OPENING_SKEW = timedelta(minutes=5)


@dataclass
class Item:
    step: str
    subject: str
    status: str  # MATCH | MISMATCH | IMPORTED | RESOLVED
    internal: str | None = None
    broker: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconResult:
    run_id: uuid.UUID
    passed: bool
    mismatches: tuple[str, ...]
    imported: int


class Reconciler:
    def __init__(
        self, db: Database, clock: Clock, brokers: BrokerRegistry, execution: ExecutionService
    ) -> None:
        self.db = db
        self.clock = clock
        self.brokers = brokers
        self.execution = execution

    async def reconcile(self, account_id: uuid.UUID, *, trigger: str) -> ReconResult:
        started = self.clock.now()
        run_id = uuid7()
        async with self.db.transaction() as conn:
            account = await get_account(conn, account_id=account_id)
            await conn.execute(
                reconciliation_runs.insert().values(
                    id=run_id,
                    account_id=account_id,
                    trigger=trigger[:100],
                    started_at=started,
                    status="RUNNING",
                )
            )
        broker = self.brokers.for_account(account)
        items: list[Item] = []
        try:
            items += await self._orders(account_id)
            venue_open = await broker.list_open_orders(account_id)
            venue_fills = await broker.get_fills(account_id, since=None)
            venue_balances = await broker.get_balances(account_id)
            if account.kind == "SANDBOX":
                # A test-network key can carry trades from before this account existed (manual
                # tests, an earlier account): they are not this account's fills.
                opened = account.created_at - OPENING_SKEW
                venue_fills = [f for f in venue_fills if f.filled_at >= opened]
        except (BrokerUnavailableError, BrokerAmbiguousError) as exc:
            items.append(
                Item(
                    "ACCOUNT_STATE",
                    "venue",
                    "MISMATCH",
                    detail={"error": f"venue unreachable: {exc}"},
                )
            )
            return await self._finish(run_id, account_id, items, trigger)
        async with self.db.connect() as conn:
            internal = {
                o.client_order_id: o for o in await list_orders(conn, account_id=account_id)
            }
            for vo in venue_open:
                o = internal.get(vo.client_order_id)
                if o is None:
                    items.append(
                        Item(
                            "BROKER_ORDERS",
                            vo.client_order_id,
                            "MISMATCH",
                            None,
                            vo.status.value,
                            {"problem": "order open at venue but unknown internally"},
                        )
                    )
                elif o.status not in ACTIVE_STATES:
                    items.append(
                        Item(
                            "BROKER_ORDERS",
                            vo.client_order_id,
                            "MISMATCH",
                            o.status.value,
                            vo.status.value,
                            {"problem": "venue still has an order we consider final"},
                        )
                    )
            # FILLS
            unique = {f.venue_fill_id: f for f in venue_fills}
            known = {
                r.broker_fill_id: r
                for r in await conn.execute(
                    select(fills.c.broker_fill_id, fills.c.quantity, fills.c.price)
                    .join(orders_t, orders_t.c.id == fills.c.order_id)
                    .where(orders_t.c.account_id == account_id)
                )
            }
        missing_orders: set[str] = set()
        for fid, vf in unique.items():
            if fid in known:
                k = known[fid]
                same = k.quantity == vf.quantity and k.price == vf.price
                items.append(
                    Item(
                        "FILLS",
                        fid,
                        "MATCH" if same else "MISMATCH",
                        f"{k.quantity}@{k.price}",
                        f"{vf.quantity}@{vf.price}",
                    )
                )
            elif vf.client_order_id in internal:
                missing_orders.add(vf.client_order_id)
            else:
                items.append(
                    Item(
                        "FILLS",
                        fid,
                        "MISMATCH",
                        None,
                        f"{vf.quantity}@{vf.price}",
                        {"problem": "fill for an order unknown internally"},
                    )
                )
        for coid in sorted(missing_orders):
            await self.execution.sync_order(internal[coid].id, source=Source.RECONCILE)
            items.append(
                Item("FILLS", coid, "IMPORTED", detail={"note": "broker-confirmed fills imported"})
            )
        for fid in known:
            if fid not in unique:
                items.append(
                    Item(
                        "FILLS",
                        fid,
                        "MISMATCH",
                        "recorded internally",
                        None,
                        {"problem": "internal fill not reported by the venue"},
                    )
                )
        async with self.db.connect() as conn:
            tracked = (
                await tracked_assets(conn, account.venue_key) if account.kind == "SANDBOX" else None
            )
            items += await self._balances(
                conn, account_id, account.base_currency, venue_balances, tracked
            )
        return await self._finish(run_id, account_id, items, trigger)

    async def _orders(self, account_id: uuid.UUID) -> list[Item]:
        out: list[Item] = []
        async with self.db.connect() as conn:
            active = await list_orders(conn, account_id=account_id, statuses=ACTIVE_STATES)
        for o in active:
            before = o.status
            after = await self.execution.sync_order(o.id, source=Source.RECONCILE)
            if after.status is OrderStatus.UNKNOWN_ORDER_STATE:
                out.append(
                    Item(
                        "BROKER_ORDERS",
                        o.client_order_id,
                        "MISMATCH",
                        before.value,
                        "unresolved",
                        {"problem": "order state still unknown; waiting for venue evidence"},
                    )
                )
            elif after.status in (OrderStatus.AUTHORIZED, OrderStatus.SUBMITTING):
                out.append(
                    Item(
                        "BROKER_ORDERS",
                        o.client_order_id,
                        "MISMATCH",
                        after.status.value,
                        None,
                        {"problem": "order transmission state unresolved"},
                    )
                )
            elif before is OrderStatus.UNKNOWN_ORDER_STATE:
                out.append(
                    Item(
                        "BROKER_ORDERS",
                        o.client_order_id,
                        "RESOLVED",
                        before.value,
                        after.status.value,
                    )
                )
            else:
                out.append(
                    Item(
                        "BROKER_ORDERS",
                        o.client_order_id,
                        "MATCH",
                        after.status.value,
                        after.status.value,
                    )
                )
        return out

    async def _balances(
        self,
        conn: AsyncConnection,
        account_id: uuid.UUID,
        quote: str,
        venue_balances: list[Any],
        tracked: set[str] | None = None,
    ) -> list[Item]:
        """Venue holdings = what the account manages + what it records as UNMANAGED. A SANDBOX
        account compares only its ``tracked`` assets (the test account holds many others)."""
        out: list[Item] = []
        venue = {b.asset: b.total for b in venue_balances}
        unmanaged = {
            asset: amount
            for (book, asset), amount in (await ledger.balances(conn, account_id)).items()
            if book == "UNMANAGED"
        }
        # POSITIONS: internal positions per base asset vs venue holdings of that asset.
        rows = await conn.execute(
            select(positions.c.quantity, instruments.c.canonical_key)
            .join(instruments, instruments.c.id == positions.c.instrument_id)
            .where(positions.c.account_id == account_id)
        )
        internal_pos: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for r in rows:
            base = r.canonical_key.split(":", 1)[1].split("/", 1)[0]
            internal_pos[base] += r.quantity
        others = (tracked if tracked is not None else set(venue)) - {quote}
        for asset in sorted(set(internal_pos) | others):
            held, outside = internal_pos.get(asset, ZERO), unmanaged.get(asset, ZERO)
            i, b = held + outside, venue.get(asset, ZERO)
            out.append(
                Item(
                    "POSITIONS",
                    asset,
                    "MATCH" if i == b else "MISMATCH",
                    str(i),
                    str(b),
                    {"position": held, "unmanaged": outside} if outside else {},
                )
            )
        # BALANCES: ledger cash (+ unmanaged) vs venue cash.
        cash = await ledger.book_balance(conn, account_id, "CASH", quote)
        outside = unmanaged.get(quote, ZERO)
        v_cash = venue.get(quote, ZERO)
        out.append(
            Item(
                "BALANCES",
                quote,
                "MATCH" if cash + outside == v_cash else "MISMATCH",
                str(cash + outside),
                str(v_cash),
                {"cash": cash, "unmanaged": outside} if outside else {},
            )
        )
        # LEDGER: positions table vs ledger POSITION book (internal consistency).
        for asset, qty in sorted(internal_pos.items()):
            book = await ledger.book_balance(conn, account_id, "POSITION", asset)
            out.append(
                Item(
                    "LEDGER",
                    asset,
                    "MATCH" if book == qty else "MISMATCH",
                    str(qty),
                    f"ledger {book}",
                )
            )
        return out

    async def _finish(
        self, run_id: uuid.UUID, account_id: uuid.UUID, items: list[Item], trigger: str
    ) -> ReconResult:
        now = self.clock.now()
        mismatches = tuple(
            f"{i.step} {i.subject}: internal={i.internal} venue={i.broker} "
            f"{i.detail.get('problem', '')}".strip()
            for i in items
            if i.status == "MISMATCH"
        )
        passed = not mismatches
        imported = sum(1 for i in items if i.status == "IMPORTED")
        async with self.db.transaction() as conn:
            for it in items:
                await conn.execute(
                    reconciliation_items.insert().values(
                        id=uuid7(),
                        run_id=run_id,
                        step=it.step,
                        subject=it.subject[:200],
                        internal_value=it.internal,
                        broker_value=it.broker,
                        status=it.status,
                        detail=to_jsonable(it.detail),
                    )
                )
            items_summary = {
                "items": len(items),
                "mismatches": len(mismatches),
                "imported": imported,
            }
            await conn.execute(
                update(reconciliation_runs)
                .where(reconciliation_runs.c.id == run_id)
                .values(
                    finished_at=now, status="PASSED" if passed else "FAILED", summary=items_summary
                )
            )
            account = await get_account(conn, account_id=account_id)
            key = f"accounting.reconciliation_failed:{account.alias}"
            unreachable_key = f"accounting.venue_unreachable:{account.alias}"
            failed = [i for i in items if i.status == "MISMATCH"]
            unreachable = bool(failed) and all(
                i.step == "ACCOUNT_STATE"
                and str(i.detail.get("error", "")).startswith("venue unreachable")
                for i in failed
            )
            if passed:
                await set_reconciliation_status(
                    conn, account_id=account_id, status="OK", now=now, reconciled=True
                )
                for k in (key, unreachable_key):
                    await resolve_by_key(
                        conn, dedup_key=k, now=now, resolution=f"reconciliation {run_id} passed"
                    )
            elif unreachable:  # nothing was compared: say so, and weigh what is at stake
                await set_reconciliation_status(
                    conn, account_id=account_id, status="FAILED", now=now
                )
                held, active = await _exposure(conn, account_id)
                at_stake = (
                    f" It holds {held} position(s) and {active} active order(s)."
                    if held or active
                    else " It holds no position and no active order."
                )
                await raise_incident(
                    conn,
                    dedup_key=unreachable_key,
                    category=Category.ACCOUNTING,
                    alert=Alert(
                        title=f"{account.alias}: venue unreachable, account not reconciled",
                        severity="CRITICAL" if held or active else "WARNING",
                        what_happened=f"Reconciliation could not reach the venue for "
                        f"{account.alias}: {failed[0].detail['error']}.",
                        verified="Nothing is known to disagree; the account could not be "
                        "checked." + at_stake,
                        uncertain="What happened at the venue meanwhile (fills, stop-outs) "
                        "until it can be reached.",
                        risk_now="New exposure on this account is blocked until it reconciles. "
                        "Positions keep the protective orders the venue holds.",
                        action_taken="Account marked reconciliation FAILED; reconciliation is "
                        "retried every 5 minutes.",
                        owner_action="Restore the connection to the venue (for HFM: the "
                        "computer, MetaTrader 5 and the bridge). Nothing needs doing at the "
                        "venue.",
                    ),
                    now=now,
                    detail={"run_id": run_id, "error": failed[0].detail["error"]},
                )
            else:
                await set_reconciliation_status(
                    conn, account_id=account_id, status="FAILED", now=now
                )
                await raise_incident(
                    conn,
                    dedup_key=key,
                    category=Category.ACCOUNTING,
                    alert=Alert(
                        title=f"Reconciliation failed for {account.alias}",
                        severity="CRITICAL",
                        what_happened=f"Our records and the venue disagree for {account.alias} "
                        f"({len(mismatches)} mismatch(es)).",
                        verified="The venue's orders, fills, positions or balances differ from "
                        "ours (details on the reconciliation run).",
                        uncertain="Which side is wrong until the items are reviewed.",
                        risk_now="New exposure on this account is blocked; existing positions "
                        "and protective orders stay as they are.",
                        action_taken="Account marked reconciliation FAILED; nothing was "
                        "changed at the venue.",
                        owner_action="Open the reconciliation run on the Portfolio page, review "
                        "each mismatch, then reconcile again. Do not resubmit orders manually.",
                    ),
                    now=now,
                    detail={"run_id": run_id, "mismatches": list(mismatches)[:20]},
                )
            await record(
                conn,
                action="reconciliation.passed" if passed else "reconciliation.failed",
                actor=ACTOR,
                occurred_at=now,
                entity_type="account",
                entity_id=account_id,
                payload={"run_id": run_id, "trigger": trigger, **items_summary},
            )
        return ReconResult(run_id, passed, mismatches, imported)


async def _exposure(conn: AsyncConnection, account_id: uuid.UUID) -> tuple[int, int]:
    """(positions held, active orders) of an account, from our own records."""
    held = int(
        await conn.scalar(
            select(func.count())
            .select_from(positions)
            .where(positions.c.account_id == account_id, positions.c.quantity > 0)
        )
        or 0
    )
    active = len(await list_orders(conn, account_id=account_id, statuses=ACTIVE_STATES))
    return held, active


async def latest_run(conn: AsyncConnection, account_id: uuid.UUID) -> Any:
    return (
        await conn.execute(
            select(reconciliation_runs)
            .where(reconciliation_runs.c.account_id == account_id)
            .order_by(reconciliation_runs.c.started_at.desc())
            .limit(1)
        )
    ).first()


async def instrument_key(conn: AsyncConnection, instrument_id: uuid.UUID) -> str:
    return (await md.instrument_by_id(conn, instrument_id)).canonical_key
