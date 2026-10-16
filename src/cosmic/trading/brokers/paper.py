"""PaperBroker: an independent simulated venue (ADR-022). Everything here is SIMULATED.

* Own books (``paper_*`` tables), separate from the internal ledger, so reconciliation
  compares two genuinely independent records.
* Fills only against fresh ingested public quotes; no quote → no fill ("cannot simulate").
* Fees, slippage and partial fills are configuration labelled ASSUMPTION (B-08).
* STOP_LIMIT orders trigger at the stop and then only fill at or better than their
  limit: in a gap they may not fill at all — exactly why a stop is not a guaranteed cap.
* No network access of any kind: paper orders cannot reach a live endpoint (AT-02).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.clock import Clock
from cosmic.core.config import DEV_ENVIRONMENTS, Settings
from cosmic.core.errors import ConflictError
from cosmic.core.ids import uuid7
from cosmic.core.labels import DataLabel
from cosmic.core.money import BPS, ZERO, is_multiple, quantize_step
from cosmic.db.engine import Database
from cosmic.db.schema_execution import paper_balances, paper_faults, paper_fills, paper_orders
from cosmic.db.schema_market import venues
from cosmic.db.schema_trading import accounts
from cosmic.market import data as md
from cosmic.trading.brokers.base import (
    AdapterCapabilities,
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
    SubmitRequest,
    SubmitResult,
    VenueBalance,
    VenueFill,
    VenueOrder,
    VenueOrderStatus,
)
from cosmic.trading.policy import active_policy

OPEN_STATES = ("OPEN", "PARTIALLY_FILLED")
FAULTS = (
    "REJECT_NEXT",
    "TIMEOUT_AFTER_ACCEPT",
    "TIMEOUT_BEFORE_ACCEPT",
    "UNAVAILABLE",
    "DUPLICATE_FILL_REPORT",
    "LOSE_CANCEL_RESPONSE",
)


@dataclass(frozen=True)
class PaperConfig:
    taker_fee_bps: Decimal = Decimal(60)  # ASSUMPTION (B-08)
    slippage_bps: Decimal = Decimal(5)  # ASSUMPTION
    max_fill_fraction: Decimal = Decimal(1)  # 1 = may fill completely on one tick
    max_quote_age_seconds: int = 120


class PaperBroker:
    def __init__(
        self, db: Database, clock: Clock, settings: Settings, config: PaperConfig | None = None
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.config = config or PaperConfig()

    def capabilities(self) -> AdapterCapabilities:
        c = self.config
        return AdapterCapabilities(
            name="paper",
            is_live=False,
            is_simulated=True,
            order_types=("LIMIT", "STOP_LIMIT"),
            time_in_force=("GTC", "GTD", "IOC"),
            idempotent_client_ids=True,
            bracket_or_oco=False,
            partial_fills=True,
            data_freshness=f"fills only on public quotes ≤ {c.max_quote_age_seconds}s old",
            precision_source="instrument master (Coinbase product metadata)",
            rate_limits="none (in-process)",
            fees=f"taker {c.taker_fee_bps} bps ASSUMPTION",
            venue_status="mirrors the real venue's status page",
            reconciliation="get_order, list_open_orders, get_fills, get_balances",
        )

    # ---- fault injection (TEST / LOCAL only) ------------------------------------------

    async def inject_fault(self, fault: str, count: int = 1) -> None:
        if self.settings.env not in DEV_ENVIRONMENTS or not self.settings.paper_fault_injection:
            raise ConflictError("paper fault injection is disabled in this environment")
        if fault not in FAULTS:
            raise ConflictError(f"unknown fault {fault}")
        async with self.db.transaction() as conn:
            await conn.execute(
                paper_faults.insert().values(
                    id=uuid7(), fault=fault, remaining=count, created_at=self.clock.now()
                )
            )

    async def _take_fault(self, conn: AsyncConnection, fault: str) -> bool:
        if not self.settings.paper_fault_injection or self.settings.env not in DEV_ENVIRONMENTS:
            return False
        row = (
            await conn.execute(
                select(paper_faults)
                .where(paper_faults.c.fault == fault, paper_faults.c.remaining > 0)
                .order_by(paper_faults.c.created_at)
                .limit(1)
                .with_for_update()
            )
        ).first()
        if row is None:
            return False
        await conn.execute(
            update(paper_faults)
            .where(paper_faults.c.id == row.id)
            .values(remaining=row.remaining - 1)
        )
        return True

    # ---- venue accounts -----------------------------------------------------------------

    async def fund(
        self, conn: AsyncConnection, account_ref: uuid.UUID, asset: str, amount: Decimal
    ) -> None:
        """Credit simulated funds at the paper venue (called when a paper account is created)."""
        now = self.clock.now()
        row = (
            await conn.execute(
                select(paper_balances).where(
                    paper_balances.c.account_ref == account_ref, paper_balances.c.asset == asset
                )
            )
        ).first()
        if row is None:
            await conn.execute(
                paper_balances.insert().values(
                    account_ref=account_ref, asset=asset, total=amount, hold=ZERO, updated_at=now
                )
            )
        else:
            await conn.execute(
                update(paper_balances)
                .where(paper_balances.c.account_ref == account_ref, paper_balances.c.asset == asset)
                .values(total=row.total + amount, updated_at=now)
            )

    async def _balance(self, conn: AsyncConnection, account_ref: uuid.UUID, asset: str) -> Any:
        row = (
            await conn.execute(
                select(paper_balances)
                .where(paper_balances.c.account_ref == account_ref, paper_balances.c.asset == asset)
                .with_for_update()
            )
        ).first()
        if row is None:
            await conn.execute(
                paper_balances.insert().values(
                    account_ref=account_ref,
                    asset=asset,
                    total=ZERO,
                    hold=ZERO,
                    updated_at=self.clock.now(),
                )
            )
            row = (
                await conn.execute(
                    select(paper_balances)
                    .where(
                        paper_balances.c.account_ref == account_ref, paper_balances.c.asset == asset
                    )
                    .with_for_update()
                )
            ).one()
        return row

    async def _adjust(
        self,
        conn: AsyncConnection,
        account_ref: uuid.UUID,
        asset: str,
        *,
        total: Decimal = ZERO,
        hold: Decimal = ZERO,
    ) -> None:
        row = await self._balance(conn, account_ref, asset)
        new_total, new_hold = row.total + total, row.hold + hold
        if new_total < ZERO or new_hold < ZERO or new_hold > new_total:
            raise ConflictError(f"paper venue balance invariant violated for {asset}")
        await conn.execute(
            update(paper_balances)
            .where(paper_balances.c.account_ref == account_ref, paper_balances.c.asset == asset)
            .values(total=new_total, hold=new_hold, updated_at=self.clock.now())
        )

    # ---- adapter API --------------------------------------------------------------------

    async def submit_order(self, account_ref: uuid.UUID, req: SubmitRequest) -> SubmitResult:
        now = self.clock.now()
        # Faults are consumed in their own committed transaction, then raised.
        async with self.db.transaction() as conn:
            unavailable = await self._take_fault(conn, "UNAVAILABLE")
            timeout_before = not unavailable and await self._take_fault(
                conn, "TIMEOUT_BEFORE_ACCEPT"
            )
        if unavailable:
            raise BrokerUnavailableError("paper venue unavailable (injected fault)")
        if timeout_before:
            raise BrokerAmbiguousError("timeout before the venue responded (injected fault)")
        async with self.db.transaction() as conn:
            existing = (
                await conn.execute(
                    select(paper_orders).where(
                        paper_orders.c.client_order_id == req.client_order_id
                    )
                )
            ).first()
            if existing is not None:  # idempotent client order IDs
                return SubmitResult(
                    existing.status != "REJECTED",
                    existing.venue_order_id,
                    existing.reject_reason,
                    {"duplicate_client_order_id": "true"},
                )
            reason = await self._validate(conn, account_ref, req)
            if reason is None and await self._take_fault(conn, "REJECT_NEXT"):
                reason = "rejected by venue (injected fault)"
            venue_id = f"pv-{uuid7().hex[:20]}"
            hold_asset, hold_amount = self._hold_for(
                req, await self._fee_bps(conn, req.instrument_key)
            )
            status = "REJECTED" if reason else "OPEN"
            await conn.execute(
                paper_orders.insert().values(
                    id=uuid7(),
                    account_ref=account_ref,
                    client_order_id=req.client_order_id,
                    venue_order_id=venue_id,
                    instrument_key=req.instrument_key,
                    side=req.side,
                    order_type=req.order_type,
                    quantity=req.quantity,
                    limit_price=req.limit_price,
                    stop_price=req.stop_price,
                    time_in_force=req.time_in_force,
                    expires_at=req.expires_at,
                    status=status,
                    hold_asset=None if reason else hold_asset,
                    hold_amount=ZERO if reason else hold_amount,
                    reject_reason=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
            if not reason:
                await self._adjust(conn, account_ref, hold_asset, hold=hold_amount)
            lose_response = (not reason) and await self._take_fault(conn, "TIMEOUT_AFTER_ACCEPT")
        if lose_response:
            raise BrokerAmbiguousError(
                "venue accepted the order but the response was lost (injected fault)"
            )
        return SubmitResult(reason is None, venue_id, reason, {"venue": "paper"})

    async def _fee_bps(self, conn: AsyncConnection, instrument_key: str) -> Decimal:
        """The owner's fee for this venue when the risk policy sets one (B-08), so simulated
        fills charge what proposals estimate; otherwise the simulator's own assumption."""
        venue = instrument_key.rsplit("@", 1)[-1]
        try:
            _, policy = await active_policy(conn)
        except ConflictError:
            return self.config.taker_fee_bps
        return next(
            (f.bps for f in policy.venue_fees if f.venue == venue), self.config.taker_fee_bps
        )

    def _hold_for(self, req: SubmitRequest, fee_bps: Decimal) -> tuple[str, Decimal]:
        base, quote = _assets(req.instrument_key)
        if req.side == "BUY":
            return quote, req.quantity * req.limit_price * (1 + fee_bps / BPS)
        return base, req.quantity

    async def _validate(
        self, conn: AsyncConnection, account_ref: uuid.UUID, req: SubmitRequest
    ) -> str | None:
        try:
            inst = await md.instrument_by_key(conn, req.instrument_key)
        except Exception:
            return "unknown instrument"
        if not inst.tradable or inst.status not in ("ONLINE", "LIMIT_ONLY"):
            return f"instrument not tradable ({inst.status})"
        if req.order_type not in ("LIMIT", "STOP_LIMIT"):
            return "unsupported order type"
        if req.order_type == "STOP_LIMIT" and (req.stop_price is None or req.side != "SELL"):
            return "stop-limit orders must be sells with a stop price"
        if inst.lot_size is None or inst.tick_size is None:
            return "instrument precision unknown"
        if not is_multiple(req.quantity, inst.lot_size) or not is_multiple(
            req.limit_price, inst.tick_size
        ):
            return "quantity or price not on lot/tick grid"
        if inst.min_notional is not None and req.quantity * req.limit_price < inst.min_notional:
            return "below minimum notional"
        asset, amount = self._hold_for(req, await self._fee_bps(conn, req.instrument_key))
        bal = await self._balance(conn, account_ref, asset)
        if bal.total - bal.hold < amount:
            return f"insufficient {asset} balance"
        return None

    async def get_order(self, account_ref: uuid.UUID, client_order_id: str) -> VenueOrder:
        async with self.db.connect() as conn:
            row = (
                await conn.execute(
                    select(paper_orders).where(
                        paper_orders.c.account_ref == account_ref,
                        paper_orders.c.client_order_id == client_order_id,
                    )
                )
            ).first()
        if row is None:
            raise BrokerOrderNotFoundError(client_order_id)
        return _venue_order(row)

    async def list_open_orders(self, account_ref: uuid.UUID) -> list[VenueOrder]:
        async with self.db.connect() as conn:
            rows = await conn.execute(
                select(paper_orders)
                .where(
                    paper_orders.c.account_ref == account_ref,
                    paper_orders.c.status.in_(OPEN_STATES),
                )
                .order_by(paper_orders.c.created_at)
            )
            return [_venue_order(r) for r in rows]

    async def cancel_order(self, account_ref: uuid.UUID, client_order_id: str) -> None:
        async with self.db.transaction() as conn:
            row = (
                await conn.execute(
                    select(paper_orders)
                    .where(
                        paper_orders.c.account_ref == account_ref,
                        paper_orders.c.client_order_id == client_order_id,
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                raise BrokerOrderNotFoundError(client_order_id)
            if row.status in OPEN_STATES:
                await self._close(conn, row, "CANCELLED")
            lose = await self._take_fault(conn, "LOSE_CANCEL_RESPONSE")
        if lose:
            raise BrokerAmbiguousError("cancel response lost (injected fault)")

    async def get_fills(self, account_ref: uuid.UUID, since: datetime | None) -> list[VenueFill]:
        async with self.db.transaction() as conn:
            q = (
                select(paper_fills, paper_orders.c.client_order_id)
                .join(paper_orders, paper_orders.c.id == paper_fills.c.paper_order_id)
                .where(paper_orders.c.account_ref == account_ref)
            )
            if since is not None:
                q = q.where(paper_fills.c.filled_at >= since)
            rows = list(await conn.execute(q.order_by(paper_fills.c.filled_at)))
            duplicate = await self._take_fault(conn, "DUPLICATE_FILL_REPORT")
        out = [
            VenueFill(
                r.venue_fill_id,
                r.client_order_id,
                r.quantity,
                r.price,
                r.fee,
                r.fee_asset,
                r.liquidity,
                r.filled_at,
            )
            for r in rows
        ]
        return out + out if duplicate else out

    async def get_balances(self, account_ref: uuid.UUID) -> list[VenueBalance]:
        async with self.db.connect() as conn:
            rows = await conn.execute(
                select(paper_balances)
                .where(paper_balances.c.account_ref == account_ref)
                .order_by(paper_balances.c.asset)
            )
            return [VenueBalance(r.asset, r.total, r.total - r.hold) for r in rows]

    async def venue_status(self) -> str:
        """The worst fresh status among the venues active paper accounts trade on (their
        prices drive the simulation); UNKNOWN when none is fresh."""
        async with self.db.connect() as conn:
            keys = sorted(
                {
                    r.key
                    for r in await conn.execute(
                        select(venues.c.key)
                        .join(accounts, accounts.c.venue_id == venues.c.id)
                        .where(accounts.c.kind == "PAPER", accounts.c.status == "ACTIVE")
                    )
                }
            )
            found = [await md.latest_venue_status(conn, k, self.clock.now()) for k in keys]
        if not found or any(label not in (DataLabel.DELAYED, DataLabel.LIVE) for _, label in found):
            return "UNKNOWN"
        statuses = {status for status, _ in found}
        return (
            "OPERATIONAL" if statuses == {"OPERATIONAL"} else sorted(statuses - {"OPERATIONAL"})[0]
        )

    # ---- matching engine ----------------------------------------------------------------

    async def tick(self) -> int:
        """Match open paper orders against the latest fresh quotes. Returns fills created."""
        now = self.clock.now()
        created = 0
        async with self.db.transaction() as conn:
            rows = list(
                await conn.execute(
                    select(paper_orders)
                    .where(paper_orders.c.status.in_(OPEN_STATES))
                    .order_by(paper_orders.c.created_at)
                    .with_for_update()
                )
            )
            for row in rows:
                if row.expires_at is not None and row.expires_at <= now:
                    await self._close(conn, row, "EXPIRED")
                    continue
                inst = await md.instrument_by_key(conn, row.instrument_key)
                quote = await md.latest_quote(conn, inst.id, now)
                if (
                    quote.label is not DataLabel.DELAYED
                    or quote.bid is None
                    or quote.ask is None
                    or quote.age_seconds is None
                    or quote.age_seconds > self.config.max_quote_age_seconds
                ):
                    continue  # cannot simulate without a fresh quote
                price = self._match_price(row, quote.bid, quote.ask)
                if price is None:
                    if row.time_in_force == "IOC":
                        await self._close(conn, row, "EXPIRED")
                    continue
                if row.order_type == "STOP_LIMIT" and not row.triggered:
                    await conn.execute(
                        update(paper_orders)
                        .where(paper_orders.c.id == row.id)
                        .values(triggered=True, updated_at=now)
                    )
                remaining = row.quantity - row.filled_qty
                lot = inst.lot_size or Decimal("0.00000001")
                qty = min(
                    remaining,
                    max(
                        lot,
                        quantize_step(row.quantity * self.config.max_fill_fraction, lot, "down"),
                    ),
                )
                await self._fill(conn, row, qty, price, now)
                created += 1
                if row.time_in_force == "IOC":
                    fresh = (
                        await conn.execute(select(paper_orders).where(paper_orders.c.id == row.id))
                    ).one()
                    if fresh.status in OPEN_STATES:
                        await self._close(conn, fresh, "EXPIRED")
        return created

    def _match_price(self, row: Any, bid: Decimal, ask: Decimal) -> Decimal | None:
        slip = self.config.slippage_bps / BPS
        if row.side == "BUY":
            if ask > row.limit_price:
                return None
            limit: Decimal = row.limit_price
            return min(limit, ask * (1 + slip)).quantize(Decimal("0.00000001"))
        if row.order_type == "STOP_LIMIT" and not row.triggered and bid > row.stop_price:
            return None  # stop not triggered
        if bid < row.limit_price:
            return None  # e.g. gapped below the stop's limit: a stop-limit may not fill
        floor: Decimal = row.limit_price
        return max(floor, bid * (1 - slip)).quantize(Decimal("0.00000001"))

    async def _fill(
        self, conn: AsyncConnection, row: Any, qty: Decimal, price: Decimal, now: datetime
    ) -> None:
        base, quote = _assets(row.instrument_key)
        fee_bps = await self._fee_bps(conn, row.instrument_key)
        fee = (qty * price * fee_bps / BPS).quantize(Decimal("0.00000001"))
        filled = row.filled_qty + qty
        hold_release = row.hold_amount * qty / row.quantity
        if row.side == "BUY":
            await self._adjust(
                conn, row.account_ref, quote, total=-(qty * price + fee), hold=-hold_release
            )
            await self._adjust(conn, row.account_ref, base, total=qty)
        else:
            await self._adjust(conn, row.account_ref, base, total=-qty, hold=-hold_release)
            await self._adjust(conn, row.account_ref, quote, total=qty * price - fee)
        status = "FILLED" if filled >= row.quantity else "PARTIALLY_FILLED"
        await conn.execute(
            update(paper_orders)
            .where(paper_orders.c.id == row.id)
            .values(
                filled_qty=filled,
                status=status,
                hold_amount=row.hold_amount - hold_release,
                updated_at=now,
            )
        )
        await conn.execute(
            paper_fills.insert().values(
                id=uuid7(),
                paper_order_id=row.id,
                venue_fill_id=f"pf-{uuid7().hex[:20]}",
                quantity=qty,
                price=price,
                fee=fee,
                fee_asset=quote,
                liquidity="TAKER",
                filled_at=now,
            )
        )
        if status == "FILLED" and row.hold_amount - hold_release > ZERO:
            await self._adjust(
                conn, row.account_ref, row.hold_asset, hold=-(row.hold_amount - hold_release)
            )
            await conn.execute(
                update(paper_orders).where(paper_orders.c.id == row.id).values(hold_amount=ZERO)
            )

    async def _close(self, conn: AsyncConnection, row: Any, status: str) -> None:
        if row.hold_amount > ZERO and row.hold_asset:
            await self._adjust(conn, row.account_ref, row.hold_asset, hold=-row.hold_amount)
        await conn.execute(
            update(paper_orders)
            .where(paper_orders.c.id == row.id)
            .values(status=status, hold_amount=ZERO, updated_at=self.clock.now())
        )

    async def force_fill_for_test(self, client_order_id: str, qty: Decimal, price: Decimal) -> None:
        """Deterministic fill (TEST/LOCAL fault-injection environments only)."""
        if self.settings.env not in DEV_ENVIRONMENTS or not self.settings.paper_fault_injection:
            raise ConflictError("forced fills are disabled in this environment")
        async with self.db.transaction() as conn:
            row = (
                await conn.execute(
                    select(paper_orders)
                    .where(paper_orders.c.client_order_id == client_order_id)
                    .with_for_update()
                )
            ).one()
            await self._fill(conn, row, qty, price, self.clock.now())


def _assets(instrument_key: str) -> tuple[str, str]:
    # Canonical key format: SPOT:BASE/QUOTE@VENUE
    pair = instrument_key.split(":", 1)[1].split("@", 1)[0]
    base, quote = pair.split("/")
    return base, quote


def _venue_order(row: Any) -> VenueOrder:
    return VenueOrder(
        row.client_order_id,
        row.venue_order_id,
        row.instrument_key,
        row.side,
        VenueOrderStatus(row.status),
        row.quantity,
        row.filled_qty,
        row.limit_price,
        row.reject_reason,
    )


GRACE_NOT_FOUND = timedelta(seconds=60)
