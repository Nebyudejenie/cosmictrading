"""Trading jobs: execution cycle, paper venue matching, expiry, reconciliation, equity,
and opening test-network accounts (the worker is the only process holding testnet keys)."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.errors import ConflictError, NotFoundError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_market import instruments, venues
from cosmic.db.schema_trading import accounts, equity_snapshots, positions
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.trading import authority
from cosmic.trading.accounts import get_account
from cosmic.trading.approvals import expire_sweep
from cosmic.trading.brokers import binance_testnet
from cosmic.trading.brokers.base import BrokerAmbiguousError, BrokerUnavailableError
from cosmic.trading.brokers.mt5_bridge import ADAPTER as HFM_ADAPTER
from cosmic.trading.brokers.mt5_bridge import HfmBridgeAdapter, hfm_symbols
from cosmic.trading.context_builder import account_state
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, account_ref, environment_of
from cosmic.trading.policy import ensure_default_policy
from cosmic.trading.sandbox import open_sandbox_account
from cosmic.trading.services import TradingServices
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_SAFETY, PRIORITY_TRADING
from cosmic.worker.registry import Periodic, Registry


def services(ctx: WorkerContext) -> TradingServices:
    svc: TradingServices = ctx.services["trading"]
    return svc


async def startup(ctx: WorkerContext) -> None:
    now = ctx.clock.now()
    async with ctx.db.transaction() as conn:
        await ensure_default_policy(conn, now)
        await authority.enforce_cap(conn, settings=ctx.settings, now=now)
    if "trading" not in ctx.services:
        ctx.services["trading"] = TradingServices.build(
            ctx.db,
            ctx.clock,
            ctx.settings,
            sandbox=binance_testnet.build(ctx.settings, ctx.clock),
            hfm=HfmBridgeAdapter(ctx.db, ctx.clock, hfm_symbols()),
        )
    await services(ctx).execution.startup_recovery()


async def record_equity(ctx: WorkerContext) -> int:
    now = ctx.clock.now()
    written = 0
    async with ctx.db.transaction() as conn:
        ids = [
            r.id
            for r in await conn.execute(select(accounts.c.id).where(accounts.c.status == "ACTIVE"))
        ]
        for account_id in ids:
            acct = await get_account(conn, account_id=account_id)
            state = await account_state(conn, acct, now)
            if state.equity is None or state.cash_available is None:
                continue  # unknown valuation is not recorded as a number
            await conn.execute(
                equity_snapshots.insert().values(
                    id=uuid7(),
                    account_id=account_id,
                    taken_at=now,
                    equity=state.equity,
                    cash=state.cash_available,
                    positions_value=state.equity - state.cash_available,
                    price_label="DELAYED",
                    is_simulated=acct.kind in SIMULATED_ACCOUNT_KINDS,
                )
            )
            written += 1
    return written


async def open_sandbox(ctx: WorkerContext, p: dict[str, Any]) -> uuid.UUID | None:
    """Open the test-network account from the venue's balances; a refusal becomes an incident
    (nothing is created), so the owner sees why on the Operations page."""
    svc = services(ctx)
    alias = str(p.get("alias", ""))[:41]
    actor = Actor.user(p["user_id"]) if p.get("user_id") else Actor.system("sandbox")
    now = ctx.clock.now()
    adapter = str(p.get("adapter") or binance_testnet.ADAPTER)
    try:
        try:
            capital = Decimal(str(p.get("managed_capital", "")))
        except InvalidOperation:
            raise ValidationFailedError("the capital to manage is not a number") from None
        if adapter == HFM_ADAPTER:
            if not isinstance(svc.brokers.hfm, HfmBridgeAdapter):
                raise ConflictError("the HFM bridge adapter is not available on the worker")
            balances = await svc.brokers.hfm.get_balances(uuid.UUID(int=0))
            fingerprint = await svc.brokers.hfm.account_fingerprint()
        else:
            sandbox = svc.brokers.sandbox
            if sandbox is None:
                raise ConflictError(
                    "the Binance test-network keys are not configured on the worker"
                )
            balances = await sandbox.adapter.get_balances(uuid.UUID(int=0))
            fingerprint = sandbox.key_fingerprint
        async with ctx.db.transaction() as conn:
            account_id = await open_sandbox_account(
                conn,
                alias=alias,
                managed_capital=capital,
                balances=balances,
                key_fingerprint=fingerprint,
                now=now,
                actor=actor,
                adapter=adapter,
            )
    except (
        ConflictError,
        NotFoundError,
        ValidationFailedError,
        BrokerUnavailableError,
        BrokerAmbiguousError,
    ) as exc:
        # Named by its own venue: an HFM demo account is not the Binance test network.
        venue = environment_of("SANDBOX", adapter).label
        async with ctx.db.transaction() as conn:
            await raise_incident(
                conn,
                dedup_key=f"trading.sandbox_not_opened:{alias}",
                category=Category.TRADING,
                alert=Alert(
                    title="account was not opened",
                    severity="WARNING",
                    what_happened=f"Opening the account ({venue}) failed: {exc}",
                    verified="No account was created and no order was placed.",
                    uncertain="Nothing beyond the reason shown.",
                    risk_now="None: its funds are virtual and nothing was created.",
                    action_taken="The request was refused.",
                    owner_action="Fix the cause shown, then open the account again from "
                    "Operations.",
                    account=account_ref(alias or "(no alias)", "SANDBOX", adapter),
                ),
                now=now,
                detail={"alias": alias},
            )
        return None
    await svc.reconciler.reconcile(account_id, trigger="account_opened")
    return account_id


FINAL = ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")


async def retired_venue_exposure(ctx: WorkerContext) -> dict[str, list[str]]:
    """Positions or unfinished orders left on a retired venue (ADR-027): its prices are no
    longer fetched, so they can be neither valued nor protected. One incident per venue."""
    retired = sorted(set(ctx.settings.retired_venues))
    now = ctx.clock.now()
    found: dict[str, list[str]] = {}
    if not retired:
        return found
    async with ctx.db.connect() as conn:
        held = await conn.execute(
            select(
                venues.c.key,
                accounts.c.alias,
                accounts.c.kind,
                accounts.c.broker_adapter,
                instruments.c.canonical_key,
                positions.c.quantity,
            )
            .select_from(positions)
            .join(accounts, accounts.c.id == positions.c.account_id)
            .join(instruments, instruments.c.id == positions.c.instrument_id)
            .join(venues, venues.c.id == instruments.c.venue_id)
            .where(venues.c.key.in_(retired), positions.c.quantity != 0)
        )
        for r in held:
            ref = account_ref(r.alias, r.kind, r.broker_adapter)
            found.setdefault(r.key, []).append(f"{ref} holds {r.quantity} {r.canonical_key}")
        working = await conn.execute(
            select(
                venues.c.key,
                accounts.c.alias,
                accounts.c.kind,
                accounts.c.broker_adapter,
                orders_t.c.client_order_id,
                orders_t.c.status,
            )
            .select_from(orders_t)
            .join(accounts, accounts.c.id == orders_t.c.account_id)
            .join(instruments, instruments.c.id == orders_t.c.instrument_id)
            .join(venues, venues.c.id == instruments.c.venue_id)
            .where(venues.c.key.in_(retired), orders_t.c.status.notin_(FINAL))
        )
        for r in working:
            ref = account_ref(r.alias, r.kind, r.broker_adapter)
            found.setdefault(r.key, []).append(f"{ref} order {r.client_order_id} is {r.status}")
    async with ctx.db.transaction() as conn:
        for venue in retired:
            key = f"trading.retired_venue_exposure:{venue}"
            items = found.get(venue, [])
            if not items:
                await resolve_by_key(
                    conn, dedup_key=key, now=now, resolution="nothing left on the retired venue"
                )
                continue
            await raise_incident(
                conn,
                dedup_key=key,
                category=Category.TRADING,
                alert=Alert(
                    title=f"Positions or orders remain on retired venue {venue}",
                    severity="WARNING",
                    what_happened=f"{venue} is retired (no new prices), but: "
                    + "; ".join(items[:5])[:600],
                    verified="Internal positions and order records.",
                    uncertain="Their current value: the venue's prices are no longer fetched.",
                    risk_now="They can be neither valued nor protected by stops.",
                    action_taken="Nothing was changed.",
                    owner_action=f"Take {venue} out of COSMIC_RETIRED_VENUES for a moment, "
                    "close the position with an approved exit (Portfolio → Propose closing), "
                    "then retire it again.",
                ),
                now=now,
                detail={"venue": venue, "items": items[:20]},
            )
    return found


def register(registry: Registry, ctx: WorkerContext) -> None:
    ctx.services.setdefault("startup_hooks", []).append(startup)

    @registry.handler("trading.cycle")
    async def cycle(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await services(ctx).execution.run_cycle()

    @registry.handler("paper.tick")
    async def tick(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await services(ctx).paper.tick()

    @registry.handler("trading.expire")
    async def expire(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        async with ctx.db.transaction() as conn:
            await expire_sweep(conn, now=ctx.clock.now())

    @registry.handler("trading.reconcile")
    async def reconcile(ctx: WorkerContext, p: dict[str, Any]) -> None:
        svc = services(ctx)
        if p.get("account_id"):
            await svc.reconciler.reconcile(
                uuid.UUID(str(p["account_id"])), trigger=str(p.get("trigger", "manual"))
            )
        else:
            await svc.reconcile_all(trigger="scheduled")

    @registry.handler("trading.equity_snapshot")
    async def equity(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await record_equity(ctx)

    @registry.handler("trading.emergency_followup")
    async def emergency_followup(ctx: WorkerContext, p: dict[str, Any]) -> None:
        """Cancel unfilled entries (per emergency policy) and reconcile, after a portal stop."""
        svc = services(ctx)
        actor = Actor.user(p["user_id"]) if p.get("user_id") else Actor.system("killswitch")
        for order_id in p.get("entry_orders", []):
            await svc.execution.request_cancel(
                uuid.UUID(str(order_id)),
                reason="emergency stop: cancel unfilled entry",
                actor=actor,
            )
        await svc.reconcile_all(trigger="emergency_stop")

    @registry.handler("trading.cancel_order")
    async def cancel_order(ctx: WorkerContext, p: dict[str, Any]) -> None:
        await services(ctx).execution.request_cancel(
            uuid.UUID(str(p["order_id"])),
            reason="cancelled by operator",
            actor=Actor.user(p["user_id"]) if p.get("user_id") else None,
        )

    @registry.handler("trading.open_sandbox_account")
    async def sandbox_account(ctx: WorkerContext, p: dict[str, Any]) -> None:
        await open_sandbox(ctx, p)

    @registry.handler("trading.retired_venues")
    async def retired_venues_check(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await retired_venue_exposure(ctx)

    registry.every(Periodic("trading.retired_venues", 600, PRIORITY_SAFETY))
    registry.every(Periodic("trading.cycle", 5, PRIORITY_TRADING))
    registry.every(Periodic("paper.tick", 10, PRIORITY_TRADING))
    registry.every(Periodic("trading.expire", 30, PRIORITY_SAFETY))
    registry.every(Periodic("trading.reconcile", 300, PRIORITY_SAFETY))
    registry.every(Periodic("trading.equity_snapshot", 300, PRIORITY_TRADING))
