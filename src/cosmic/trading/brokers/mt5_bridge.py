"""HFM through a MetaTrader 5 bridge (ADR-028): the worker's adapter and shared names.

The bridge program (``bridge/cosmic_mt5_bridge.py``) runs on Windows next to the MT5 terminal
and syncs with the portal; this adapter never touches the network. It:
- turns order operations into bridge commands (``bridge_commands``) and waits for their result.
  A read that times out is "venue unavailable"; a send or cancel that times out is "outcome
  unknown" (BrokerAmbiguousError), so the order is reconciled and never resent;
- answers questions about orders, fills and balances from the bridge's latest report
  (``bridge_state``), which must be fresh and from a demo account;
- shows the hedging CFD account as fully paid spot: USD cash = balance − Σ open (volume × contract
  × open price), holdings = Σ open volume × contract per symbol, fees = −(commission + fee +
  swap) of each deal. A buy therefore needs its full notional in cash (effective leverage ≤ 1).

MT5 times are the broker's server time; the bridge reports the offset to UTC.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.clock import Clock
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO, canonical_str
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_trading import accounts, bridge_commands, bridge_state, broker_contract_runs
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

ADAPTER = "hfm_mt5_demo"
VENUE = "HFM"
BRIDGE = "hfm-demo"
BRIDGE_2 = "hfm-demo-2"
ADAPTER_2 = "hfm_mt5_demo_2"


@dataclass(frozen=True)
class HfmBridge:
    """One MT5 terminal on its own HFM demo account, with its bridge (S74, ADR-028 addendum).
    The primary bridge's prices, candles and terminal status are HFM's market data; every other
    bridge only trades its own account (the owner's daily robot, 2026-11-10)."""

    name: str  # bridge_state / bridge_commands key, and what the bridge reports as "bridge"
    adapter: str  # accounts.broker_adapter of the HFM demo account it trades
    primary: bool


BRIDGES: dict[str, HfmBridge] = {
    BRIDGE: HfmBridge(BRIDGE, ADAPTER, True),
    BRIDGE_2: HfmBridge(BRIDGE_2, ADAPTER_2, False),
}
BY_ADAPTER: dict[str, HfmBridge] = {b.adapter: b for b in BRIDGES.values()}
ADAPTERS = frozenset(BY_ADAPTER)  # every HFM demo adapter

# The third terminal (S83): a REAL account, read-only. It is not in BRIDGES: it has no adapter, so
# no order can ever be routed to it; it only reports the account and answers sign-in commands.
BRIDGE_REAL = "hfm-real"


@dataclass(frozen=True)
class Slot:
    """One MT5 terminal on the bridge computer, as Broker Connections sees it (S83)."""

    name: str
    kind: str  # "DEMO": trades its demo account through ``adapter``; "REAL": read-only
    adapter: str | None
    label: str


SLOTS: dict[str, Slot] = {
    BRIDGE: Slot(BRIDGE, "DEMO", ADAPTER, "Terminal 1 · demo · HFM's prices"),
    BRIDGE_2: Slot(BRIDGE_2, "DEMO", ADAPTER_2, "Terminal 2 · demo"),
    BRIDGE_REAL: Slot(BRIDGE_REAL, "REAL", None, "Terminal 3 · real · read-only"),
}
# Commands about the account itself (S83, bridge version 7): sign in, or say who is signed in.
# They go to a terminal whatever account it holds; trading commands only to the opened demo.
CONNECTION_COMMANDS = frozenset({"LOGIN", "VERIFY"})


def account_fingerprint(login: int, server: str) -> str:
    """What the bridge reports instead of the account number: the first 16 hex characters of
    SHA-256 of ``login@server``, exactly as MT5 names the server (bridge ``snapshot``)."""
    return hashlib.sha256(f"{login}@{server}".encode()).hexdigest()[:16]


QUOTE_SOURCE = "hfm_mt5_bridge"
STATUS_SOURCE = "hfm_mt5_status"
QUOTE = "USD"

# Values exported by MetaTrader5 5.0.6231 (docs/integrations/hfm-mt5-demo.md).
DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
POSITION_SELL = 1  # MT5 position type of a short
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5  # HFM closed the position at its take-profit (bridge version 7 sends it)
DEAL_TRADES = (0, 1)  # BUY and SELL deals; the others are deposits, credits and the like
# MT5's magic number of a trade made by hand (desktop, phone or web): the owner's own, never the
# system's, whose orders always carry one (S59, the owner's choice of 2026-11-04).
HAND = 0
# MT5 deal reasons of a deal made by a person: the desktop terminal, the phone, the web (S71).
# A stop-loss (4) or take-profit (5) is HFM's own, even when it carries magic 0.
MANUAL_REASONS = frozenset({0, 1, 2})
ORDER_STATE_CANCELED, ORDER_STATE_FILLED, ORDER_STATE_REJECTED, ORDER_STATE_EXPIRED = 2, 4, 5, 6

STALE_AFTER = timedelta(seconds=15)
# A report up to this late may be a slow one, not an outage: a read waits a little for the next
# (on the server, 18 of 2,016 checks in the week to 2026-11-04 found it 15-17 s old).
SLOW = timedelta(seconds=30)
SLOW_WAIT = 10.0  # seconds
COMMAND_TTL = timedelta(seconds=20)
WAIT = timedelta(seconds=25)
ATTACHED_SL_VERSION = 2  # the first bridge that sends the stop-loss with the entry
RATES_VERSION = 4  # the first bridge that sends HFM's candles
SHORTS_VERSION = 5  # the first bridge that opens a short and closes it with a buy
HISTORY_VERSION = 6  # the first bridge that reads HFM's older candles (RATES "before")
# 7 (S83): signs in to the account the portal names (LOGIN), says who is signed in (VERIFY),
# checks the expected account before every order, and sends the take-profit with the entry.
LOGIN_VERSION = 7
ATTACHED_TP_VERSION = 7
# Why the bridge cannot be traded through (shared with the reconciliation's advice).
MONEY_NOT_HANDLED = "the HFM account's money is in"
OTHER_ACCOUNT = "the bridge reports a different HFM account than the one opened here"
# Runs that are not the broker contract: the HFM trade check (S56) proves HFM's behaviour on a
# demo before an account is opened, but it is not the contract that lets one be opened.
NOT_CONTRACT = "hfm-mt5-trade-%"


async def opened_on(conn: AsyncConnection, adapter: str = ADAPTER) -> str | None:
    """The fingerprint of the MT5 account the open HFM account of ``adapter`` was opened on: the
    passed broker contract that let it open. None while no such account is open, or when that is
    unrecorded."""
    created = await conn.scalar(
        select(accounts.c.created_at).where(
            accounts.c.broker_adapter == adapter,
            accounts.c.kind == "SANDBOX",
            accounts.c.status == "ACTIVE",
        )
    )
    if created is None:
        return None
    fp: str | None = await conn.scalar(
        select(broker_contract_runs.c.key_fingerprint)
        .where(
            broker_contract_runs.c.adapter == adapter,
            broker_contract_runs.c.status == "PASSED",
            broker_contract_runs.c.code_version.not_like(NOT_CONTRACT),
            broker_contract_runs.c.finished_at <= created + timedelta(minutes=5),
        )
        .order_by(desc(broker_contract_runs.c.finished_at))
        .limit(1)
    )
    return fp


def report_problem(
    snap: Mapping[str, Any], received_at: datetime, now: datetime, expected: str | None
) -> str | None:
    """Why the bridge's report cannot be traded through, in words; None when it can. The one
    rule for the adapter, and for whatever offers a trade on it. ``expected``: ``opened_on``."""
    age = now - received_at
    if age > STALE_AFTER:
        return f"the HFM bridge last reported {int(age.total_seconds())} s ago"
    account = snap.get("account") or {}
    if account.get("trade_mode") != "DEMO":
        return "the MT5 terminal is not on a demo account"
    if account.get("currency") != QUOTE:
        # Fail closed (S55): a Cent account keeps its money in US cents, which would be read as
        # dollars; nothing is sent or reconciled until that is handled.
        money = account.get("currency") or "an unknown currency"
        return f"{MONEY_NOT_HANDLED} {money}; only {QUOTE} accounts are handled here yet"
    if expected is not None and account.get("fingerprint") != expected:
        # Fail closed (S56 review): MT5 logged into another account would trade it, and its deals
        # would be read as this account's.
        return OTHER_ACCOUNT
    return None


def trading_off(snap: Mapping[str, Any]) -> str | None:
    """Why MT5 would refuse an order sent now (None when it would not): not connected to HFM, or
    Algo Trading switched off. The report is still read; only orders are refused."""
    terminal = snap.get("terminal") or {}
    if not terminal.get("connected"):
        return "MT5 has lost its link to HFM"
    if terminal.get("trade_allowed") is not True or terminal.get("tradeapi_disabled") is True:
        return "Algo Trading is off in MT5"
    return None


@dataclass(frozen=True)
class OwnDeal:
    """A deal of a trade the owner made by hand: outside the system's money."""

    ticket: str
    at: datetime
    symbol: str
    result: Decimal  # profit, commission, swap and fee, as HFM booked them


def by_hand(snap: Mapping[str, Any]) -> tuple[set[Any], set[Any]]:
    """(deal tickets, open position tickets) of the trades the owner made by hand.

    A position is the owner's when it was opened without the system's magic number. Its deals are
    decided by that opening, not by their own magic: HFM can close the system's own position by
    its stop-loss with a deal carrying magic 0. A deal by hand on the system's own position (a
    partial close from the phone) stays the system's, so the check with HFM shows it."""
    deals = snap.get("deals") or []
    positions = snap.get("positions") or []
    ours = {d["position_id"] for d in deals if d["entry"] == DEAL_ENTRY_IN and d["magic"] != HAND}
    ours |= {p["ticket"] for p in positions if p["magic"] != HAND}
    own_deals = {
        d["ticket"]
        for d in deals
        if d["magic"] == HAND and d.get("type") in DEAL_TRADES and d["position_id"] not in ours
    }
    return own_deals, {p["ticket"] for p in positions if p["magic"] == HAND}


@dataclass(frozen=True)
class HandClose:
    """The owner closed (part of) the system's own position by hand in MT5 (S71): HFM's deal."""

    ticket: str  # HFM's deal
    entry_coid: str  # the system entry whose position it closed
    quantity: Decimal
    price: Decimal
    fee: Decimal  # commission, fee and swap HFM charged on it (never negative)
    filled_at: datetime
    # "HAND": a person closed it (S71); "TAKE_PROFIT": HFM closed it at the take-profit the entry
    # carried (S83, bridge 7). Either way it is HFM's own deal, booked as the trade's exit.
    by: str = "HAND"


def hfm_symbols() -> dict[str, str]:
    """Instrument key → MT5 symbol for every HFM instrument in the registry."""
    from cosmic.sources.registry import INSTRUMENTS

    return {d.canonical_key: d.listings[QUOTE_SOURCE] for d in INSTRUMENTS if d.venue_key == VENUE}


def magic(client_order_id: str) -> int:
    """The MT5 ``magic`` number for one of our orders: the first 63 bits of SHA-256 of its client
    order ID. Orders, deals and positions carry it, so an order is found again without guessing
    (docs/integrations/mt5-python-api.md facts 15, 26)."""
    return int.from_bytes(hashlib.sha256(client_order_id.encode()).digest()[:8], "big") >> 1


def first_stop(entry_client_order_id: str) -> str:
    """The protection step's first stop for an entry: ``ct-<trade>-E`` → ``ct-<trade>-SL1``."""
    return f"{entry_client_order_id.removesuffix('-E')}-SL1"


def bridge_version(snap: Mapping[str, Any]) -> int:
    try:
        return int(str(snap.get("version") or "0"))
    except ValueError:
        return 0


def purpose_of(client_order_id: str) -> tuple[str, str]:
    """(purpose, the entry's client order ID) from our ID: ``…-E``, ``…-SL1``, ``…-TP1``,
    ``…-X1`` (orders of one trade share the trade code)."""
    head, _, tail = client_order_id.rpartition("-")
    if tail == "E":
        return "ENTRY", client_order_id
    for prefix, purpose in (("SL", "STOP_LOSS"), ("TP", "TAKE_PROFIT"), ("X", "EXIT")):
        if tail.startswith(prefix) and tail[len(prefix) :].isdigit():
            return purpose, f"{head}-E"
    raise BrokerOrderNotFoundError(client_order_id)


def _d(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


@dataclass(frozen=True)
class OrderIds:
    by_magic: dict[int, str]  # magic → our client order ID
    stop_of: dict[str, str]  # entry ID → its latest stop's ID
    attached: dict[str, Decimal]  # entry ID → the stop-loss sent with it
    stop_sent: frozenset[str]  # entries whose stop the protection step has sent


class HfmBridgeAdapter:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        symbols: Mapping[str, str],
        *,
        bridge: str = BRIDGE,
        wait: timedelta = WAIT,
        poll_seconds: float = 0.25,
        slow_wait: float = SLOW_WAIT,
    ) -> None:
        self.db = db
        self.clock = clock
        self.symbols = dict(symbols)  # instrument key → MT5 symbol
        self._instrument = {v: k for k, v in self.symbols.items()}
        self.bridge = bridge
        self.adapter = BRIDGES[bridge].adapter  # the HFM demo account's adapter it serves
        self.wait = wait
        self.poll_seconds = poll_seconds
        self.slow_wait = slow_wait

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            name=self.adapter,
            is_live=False,
            is_simulated=True,  # an MT5 demo account: virtual funds at the broker
            order_types=("LIMIT", "STOP_LIMIT"),
            time_in_force=("GTC", "IOC"),
            idempotent_client_ids=True,
            bracket_or_oco=False,
            attached_stop=True,  # the position's sl travels with the buy (bridge version 2)
            stops_per_position=True,  # a STOP_LOSS sets or clears the sl of the entry's positions
            stop_fills_at_market=True,  # MT5's sl closes at market once hit
            shorts=True,  # a SELL entry opens a short (bridge version 5)
            attached_take_profit=True,  # the position's tp travels with it (bridge version 7)
            partial_fills=True,
            data_freshness="the bridge's last report (at most 15 s old)",
            precision_source="symbol_info via the bridge (volume step × contract size)",
            rate_limits="one command at a time per bridge sync (about 1 s)",
            fees="commission, fee and swap per deal, as MT5 reports them (demo)",
            venue_status="terminal connected, trading allowed, demo account",
            reconciliation="magic number per order; deals and positions from the bridge",
        )

    # ---- the bridge's report -----------------------------------------------------------------

    async def _row(self) -> Any:
        async with self.db.connect() as conn:
            return (
                await conn.execute(select(bridge_state).where(bridge_state.c.bridge == self.bridge))
            ).first()

    async def _state(self) -> tuple[dict[str, Any], datetime]:
        row = await self._row()
        if row is None:
            raise BrokerUnavailableError("the HFM bridge has never connected")
        if STALE_AFTER < self.clock.now() - row.received_at <= SLOW:
            # Slow, perhaps not silent: wait a little for the next report. A report older than
            # SLOW is an outage, refused at once.
            deadline = time.monotonic() + self.slow_wait
            while time.monotonic() < deadline:
                await asyncio.sleep(self.poll_seconds)
                row = await self._row()
                if self.clock.now() - row.received_at <= STALE_AFTER:
                    break
        async with self.db.connect() as conn:
            expected = await opened_on(conn, self.adapter)
        problem = report_problem(row.snapshot, row.received_at, self.clock.now(), expected)
        if problem is not None:
            raise BrokerUnavailableError(problem)
        return row.snapshot, row.received_at

    async def _expected_account(self) -> str | None:
        """The account every order of this adapter is for: the fingerprint of the MT5 account its
        HFM account was opened on. Bridge 7 checks it again just before sending (S83)."""
        async with self.db.connect() as conn:
            return await opened_on(conn, self.adapter)

    def _contract(self, snap: Mapping[str, Any], symbol: str) -> Decimal:
        spec = (snap.get("symbols") or {}).get(symbol)
        if not spec:
            raise BrokerUnavailableError(f"the bridge has not reported {symbol}")
        return _d(spec["contract_size"])

    def _utc(self, snap: Mapping[str, Any], ms: Any) -> datetime:
        offset = snap.get("server_utc_offset_ms")
        if offset is None:
            raise BrokerUnavailableError("the broker's clock offset is not known yet")
        return datetime.fromtimestamp((int(ms) - int(offset)) / 1000, tz=UTC)

    # ---- commands ----------------------------------------------------------------------------

    async def _command(
        self, kind: str, payload: dict[str, Any], *, mutating: bool, keep: bool = True
    ) -> Any:
        """``keep=False``: a read-only answer kept elsewhere (HFM's candles are stored as
        candles) is not kept twice: once read, the command's result shrinks to its size."""
        command_id = uuid7()
        now = self.clock.now()
        async with self.db.transaction() as conn:
            await conn.execute(
                bridge_commands.insert().values(
                    id=command_id,
                    bridge=self.bridge,
                    kind=kind,
                    payload=payload,
                    status="PENDING",
                    created_at=now,
                    expires_at=now + COMMAND_TTL,
                )
            )
        deadline = time.monotonic() + self.wait.total_seconds()
        while True:
            async with self.db.connect() as conn:
                row = (
                    await conn.execute(
                        select(bridge_commands).where(bridge_commands.c.id == command_id)
                    )
                ).one()
            if row.status == "DONE":
                result = row.result or {}
                if not keep:
                    async with self.db.transaction() as conn:
                        await conn.execute(
                            update(bridge_commands)
                            .where(bridge_commands.c.id == command_id)
                            .values(
                                result={
                                    "kept_as": "candles",
                                    "rates": len(result.get("rates") or []),
                                }
                            )
                        )
                return result
            if row.status == "FAILED":
                return {"failed": row.error or "failed at the bridge"}
            if row.status == "EXPIRED":  # never handed out: nothing was sent
                raise BrokerUnavailableError("the HFM bridge did not take the request in time")
            if time.monotonic() > deadline:
                if mutating and row.status == "SENT":
                    raise BrokerAmbiguousError("the HFM bridge did not answer: outcome unknown")
                raise BrokerUnavailableError("the HFM bridge did not answer in time")
            await asyncio.sleep(self.poll_seconds)

    async def _entry_of(self, client_order_id: str, fallback: str) -> str:
        """The client order ID of the entry an order belongs to, from the order's own record.
        Exit orders carry the exit's trade code but are attached to the entry they close, so
        the entry cannot be read from the order's name; protective orders share the entry's."""
        async with self.db.connect() as conn:
            code = await conn.scalar(
                select(trade_intents.c.trade_code)
                .join(orders_t, orders_t.c.intent_id == trade_intents.c.id)
                .where(orders_t.c.client_order_id == client_order_id)
            )
        return f"ct-{code}-E" if code else fallback

    async def _submits(self) -> list[Any]:
        """Our SUBMIT commands (newest first): payload and result per client order ID."""
        async with self.db.connect() as conn:
            return list(
                await conn.execute(
                    select(bridge_commands)
                    .where(
                        bridge_commands.c.bridge == self.bridge, bridge_commands.c.kind == "SUBMIT"
                    )
                    .order_by(desc(bridge_commands.c.created_at))
                )
            )

    # ---- the contract ------------------------------------------------------------------------

    async def submit_order(self, account_ref: uuid.UUID, req: SubmitRequest) -> SubmitResult:
        symbol = self.symbols.get(req.instrument_key)
        if symbol is None:
            return SubmitResult(False, None, f"{req.instrument_key} is not traded at HFM")
        purpose, guess = purpose_of(req.client_order_id)
        entry = await self._entry_of(req.client_order_id, guess)
        snap, _ = await self._state()
        if purpose == "ENTRY":  # identity, again, just before a new position is sent (S83)
            from cosmic.trading import connections  # built on this module: imported here

            async with self.db.connect() as conn:
                problem = await connections.entry_problem(conn, self.bridge, self.clock.now())
            if problem is not None:
                return SubmitResult(False, None, f"not sent: {problem}")
        lots = req.quantity / self._contract(snap, symbol)
        payload = {
            "client_order_id": req.client_order_id,
            "symbol": symbol,
            "side": req.side,
            "purpose": purpose,
            "order_type": req.order_type,
            "volume": canonical_str(lots),
            "limit_price": canonical_str(req.limit_price),
            "stop_price": canonical_str(req.stop_price) if req.stop_price is not None else None,
            "time_in_force": req.time_in_force,
            "entry_client_order_id": entry,
        }
        if purpose == "ENTRY" and req.side == "SELL" and bridge_version(snap) < SHORTS_VERSION:
            return SubmitResult(  # an older bridge would refuse it, or worse: nothing is sent
                False,
                None,
                f"the HFM bridge (version {snap.get('version')}) cannot open a short; update the "
                f"bridge program to version {SHORTS_VERSION}",
            )
        expected = await self._expected_account()
        if expected is not None:  # bridge 7 refuses the order on any other account (S83)
            payload["expected_account"] = expected
        if (
            purpose == "ENTRY"
            and req.attached_take_profit is not None
            and bridge_version(snap) >= ATTACHED_TP_VERSION
        ):  # an older bridge would drop it: the protection step then keeps watching it
            payload["attached_tp"] = canonical_str(req.attached_take_profit)
        if purpose == "ENTRY" and req.attached_stop is not None:
            if bridge_version(snap) < ATTACHED_SL_VERSION:  # nothing is sent
                return SubmitResult(
                    False,
                    None,
                    f"the HFM bridge (version {snap.get('version')}) cannot send the stop-loss "
                    f"with the entry; update the bridge program to version {ATTACHED_SL_VERSION}",
                )
            payload["attached_sl"] = canonical_str(req.attached_stop)
        res = await self._command("SUBMIT", payload, mutating=True)
        status = res.get("status")
        if "failed" in res:
            return SubmitResult(False, None, str(res["failed"])[:200])
        if status in ("FILLED", "PARTIAL", "PLACED", "ENDED", "EXPIRED"):
            venue_id = res.get("order") or ",".join(map(str, res.get("positions") or [])) or "ioc"
            return SubmitResult(True, str(venue_id), None, {"status": str(status)})
        if status == "REJECTED":
            reason = f"{res.get('retcode') or ''} {res.get('comment') or ''}".strip()
            return SubmitResult(False, None, reason or "rejected by HFM")
        raise BrokerAmbiguousError(f"HFM outcome unknown ({status})")

    async def get_order(self, account_ref: uuid.UUID, client_order_id: str) -> VenueOrder:
        purpose, entry = purpose_of(client_order_id)
        snap, received_at = await self._state()
        cmd = next(
            (
                c
                for c in await self._submits()
                if (c.payload or {}).get("client_order_id") == client_order_id
            ),
            None,
        )
        if cmd is None or cmd.status == "EXPIRED":  # never handed to the bridge: nothing sent
            raise BrokerOrderNotFoundError(client_order_id)
        if cmd.status in ("PENDING", "SENT") or (cmd.finished_at or received_at) > received_at:
            raise BrokerUnavailableError("waiting for the HFM bridge's next report")
        p = cmd.payload
        symbol = p["symbol"]
        contract = self._contract(snap, symbol)
        qty = _d(p["volume"]) * contract
        result = cmd.result or {}
        deals = snap.get("deals") or []
        reason: str | None = None
        if purpose == "ENTRY":
            m = magic(client_order_id)
            filled = sum(
                (
                    _d(d["volume"]) * contract
                    for d in deals
                    if d["magic"] == m and d["entry"] == DEAL_ENTRY_IN
                ),
                ZERO,
            )
            pending = [o for o in snap.get("orders") or [] if o["magic"] == m]
            history = [o for o in snap.get("history_orders") or [] if o["magic"] == m]
            if pending:
                status = VenueOrderStatus.PARTIALLY_FILLED if filled else VenueOrderStatus.OPEN
            elif filled >= qty:
                status = VenueOrderStatus.FILLED
            elif history and history[-1]["state"] == ORDER_STATE_CANCELED:
                status = VenueOrderStatus.CANCELLED
            elif history and history[-1]["state"] in (ORDER_STATE_REJECTED, ORDER_STATE_EXPIRED):
                status = VenueOrderStatus.EXPIRED
            elif result.get("status") == "EXPIRED" or cmd.status == "FAILED":
                status, reason = VenueOrderStatus.EXPIRED, result.get("comment") or cmd.error
            elif result.get("status") == "REJECTED":
                status, reason = VenueOrderStatus.REJECTED, result.get("comment")
            elif filled:
                status = VenueOrderStatus.PARTIALLY_FILLED
            else:
                raise BrokerAmbiguousError(f"{client_order_id} is neither open nor finished at HFM")
        elif purpose == "STOP_LOSS":
            em = magic(entry)
            positions = [p_ for p_ in snap.get("positions") or [] if p_["magic"] == em]
            entry_positions = {
                d["position_id"] for d in deals if d["magic"] == em and d["entry"] == DEAL_ENTRY_IN
            }
            stopped = [
                d
                for d in deals
                if d["reason"] == DEAL_REASON_SL and d["position_id"] in entry_positions
            ]
            filled = sum((_d(d["volume"]) * contract for d in stopped), ZERO)
            stop = _d(p.get("stop_price"))
            if result.get("status") == "REJECTED" or cmd.status == "FAILED":
                status, reason = VenueOrderStatus.REJECTED, result.get("comment") or cmd.error
            elif positions and any(_d(x["sl"]) == stop for x in positions):
                status = VenueOrderStatus.OPEN
                qty = sum((_d(x["volume"]) * contract for x in positions), ZERO) + filled
            elif filled:
                status, qty = VenueOrderStatus.FILLED, filled
            else:  # removed, or the position was closed by an exit: the stop is gone
                status = VenueOrderStatus.CANCELLED
        else:  # EXIT or TAKE_PROFIT: immediate or cancel
            m = magic(client_order_id)
            filled = sum(
                (
                    _d(d["volume"]) * contract
                    for d in deals
                    if d["magic"] == m and d["entry"] == DEAL_ENTRY_OUT
                ),
                ZERO,
            )
            if filled >= qty:
                status = VenueOrderStatus.FILLED
            elif result.get("status") == "REJECTED" or cmd.status == "FAILED":
                status, reason = VenueOrderStatus.REJECTED, result.get("comment") or cmd.error
            else:
                status = VenueOrderStatus.EXPIRED  # IOC: what did not fill at once never will
        return VenueOrder(
            client_order_id=client_order_id,
            venue_order_id=str(result.get("order") or client_order_id),
            instrument_key=self._instrument.get(symbol, symbol),
            side=p["side"],
            status=status,
            quantity=qty,
            filled_qty=filled,
            limit_price=_d(p["limit_price"]),
            reject_reason=reason,
        )

    async def cancel_order(self, account_ref: uuid.UUID, client_order_id: str) -> None:
        purpose, entry = purpose_of(client_order_id)
        if purpose in ("EXIT", "TAKE_PROFIT"):
            return  # immediate or cancel: nothing rests
        await self._state()  # a silent bridge fails fast instead of holding the cycle 25 s
        symbol = next(
            (
                c.payload["symbol"]
                for c in await self._submits()
                if (c.payload or {}).get("client_order_id") == client_order_id
            ),
            None,
        )
        if symbol is None:
            raise BrokerOrderNotFoundError(client_order_id)
        payload = {
            "client_order_id": client_order_id,
            "purpose": purpose,
            "symbol": symbol,
            "entry_client_order_id": entry,
        }
        expected = await self._expected_account()
        if expected is not None:
            payload["expected_account"] = expected
        res = await self._command("CANCEL", payload, mutating=True)
        if res.get("status") in ("CANCELLED", "NOT_OPEN"):
            return
        raise BrokerAmbiguousError(f"cancel not confirmed by HFM: {res}")

    async def _ids(self) -> OrderIds:
        """Our orders as the bridge's commands name them. A stop-loss sent with an entry is
        the entry's first stop (``…-SL1``) until the protection step sends that order, seconds
        after the fill."""
        by_magic: dict[int, str] = {}
        stop_of: dict[str, str] = {}
        attached: dict[str, Decimal] = {}
        stop_sent: set[str] = set()
        for c in reversed(await self._submits()):  # oldest first: the latest stop wins
            p = c.payload or {}
            coid = p.get("client_order_id")
            if not coid:
                continue
            by_magic[magic(coid)] = coid
            if p.get("purpose") == "STOP_LOSS":
                stop_of[p["entry_client_order_id"]] = coid
                stop_sent.add(p["entry_client_order_id"])
            elif p.get("purpose") == "ENTRY" and p.get("attached_sl") is not None:
                attached[coid] = _d(p["attached_sl"])
                stop_of.setdefault(coid, first_stop(coid))
        return OrderIds(by_magic, stop_of, attached, frozenset(stop_sent))

    async def list_open_orders(self, account_ref: uuid.UUID) -> list[VenueOrder]:
        snap, _ = await self._state()
        ids = await self._ids()
        by_magic, stop_of = ids.by_magic, ids.stop_of
        out: list[VenueOrder] = []
        _, own_positions = by_hand(snap)
        for o in snap.get("orders") or []:
            if o["magic"] == HAND:
                continue  # the owner's own order, placed by hand
            contract = self._contract(snap, o["symbol"])
            out.append(
                VenueOrder(
                    client_order_id=by_magic.get(o["magic"], f"mt5:{o['ticket']}"),
                    venue_order_id=str(o["ticket"]),
                    instrument_key=self._instrument.get(o["symbol"], o["symbol"]),
                    side="BUY",
                    status=VenueOrderStatus.OPEN,
                    quantity=_d(o["volume_current"]) * contract,
                    filled_qty=ZERO,
                    limit_price=_d(o["price_open"]),
                    reject_reason=None,
                )
            )
        for pos in snap.get("positions") or []:
            if _d(pos["sl"]) <= 0 or pos["ticket"] in own_positions:
                continue
            entry = by_magic.get(pos["magic"])
            own = ids.attached.get(entry or "")
            if own is not None and entry not in ids.stop_sent and _d(pos["sl"]) == own:
                continue  # the entry's own stop, until the protection step sends it
            stop = stop_of.get(entry or "", f"mt5:sl:{pos['ticket']}")
            contract = self._contract(snap, pos["symbol"])
            out.append(
                VenueOrder(
                    client_order_id=stop,
                    venue_order_id=f"sl:{pos['ticket']}",
                    instrument_key=self._instrument.get(pos["symbol"], pos["symbol"]),
                    side="SELL",
                    status=VenueOrderStatus.OPEN,
                    quantity=_d(pos["volume"]) * contract,
                    filled_qty=ZERO,
                    limit_price=_d(pos["sl"]),
                    reject_reason=None,
                )
            )
        return out

    async def get_fills(self, account_ref: uuid.UUID, since: datetime | None) -> list[VenueFill]:
        snap, _ = await self._state()
        ids = await self._ids()
        by_magic, stop_of = ids.by_magic, ids.stop_of
        deals = snap.get("deals") or []
        own_deals, _ = by_hand(snap)
        entry_of_position = {
            d["position_id"]: by_magic.get(d["magic"]) for d in deals if d["entry"] == DEAL_ENTRY_IN
        }
        fills: list[VenueFill] = []
        for d in deals:
            if (
                d["entry"] not in (DEAL_ENTRY_IN, DEAL_ENTRY_OUT)
                or d["symbol"] not in self._instrument
                or d["ticket"] in own_deals
            ):
                continue
            if d["reason"] == DEAL_REASON_SL:
                coid = stop_of.get(
                    entry_of_position.get(d["position_id"]) or "", f"mt5:{d['order']}"
                )
            elif d["reason"] == DEAL_REASON_TP:
                # HFM's own take-profit closed it (S83): booked as the trade's exit by the
                # reconciliation (``hand_closes``), never read as a fill of the entry itself.
                coid = f"mt5:tp:{d['order']}"
            else:
                coid = by_magic.get(d["magic"], f"mt5:{d['order']}")
            filled_at = self._utc(snap, d["time_msc"])
            if since is not None and filled_at < since:
                continue
            charges = _d(d.get("commission")) + _d(d.get("fee")) + _d(d.get("swap"))
            fills.append(
                VenueFill(
                    venue_fill_id=str(d["ticket"]),
                    client_order_id=coid,
                    quantity=_d(d["volume"]) * self._contract(snap, d["symbol"]),
                    price=_d(d["price"]),
                    fee=max(
                        -charges, ZERO
                    ),  # a net credit cannot be a fee; reconciliation shows it
                    fee_asset=QUOTE,
                    liquidity="TAKER",
                    filled_at=filled_at,
                )
            )
        fills.sort(key=lambda f: f.filled_at)
        return fills

    async def get_balances(self, account_ref: uuid.UUID) -> list[VenueBalance]:
        snap, _ = await self._state()
        account = snap.get("account") or {}
        held: dict[str, Decimal] = {}
        open_cost = ZERO
        _, own_positions = by_hand(snap)
        for pos in snap.get("positions") or []:
            if pos["ticket"] in own_positions:
                continue  # the owner's own: its money is in the balance only once it closes
            key = self._instrument.get(pos["symbol"])
            contract = self._contract(snap, pos["symbol"])
            # A short (S66) is held below zero, and its sale brought cash in: the books' terms.
            qty = _d(pos["volume"]) * contract * (-1 if pos.get("type") == POSITION_SELL else 1)
            open_cost += qty * _d(pos["price_open"])
            base = key.split(":", 1)[1].split("/", 1)[0] if key else f"mt5:{pos['symbol']}"
            held[base] = held.get(base, ZERO) + qty
        cash = _d(account.get("balance")) - open_cost
        out = [VenueBalance(QUOTE, cash, cash)]
        out += [VenueBalance(asset, qty, qty) for asset, qty in sorted(held.items())]
        return out

    async def hand_closes(self, account_ref: uuid.UUID) -> list[HandClose]:
        """Deals made by a person (the terminal, the phone, the web) that closed a position the
        system opened: the owner closed the system's trade by hand. Only from HFM's own records;
        the system's own stop-outs tagged with magic 0 are not among them."""
        snap, _ = await self._state()
        ids = await self._ids()
        deals = snap.get("deals") or []
        opened_by = {
            d["position_id"]: d["magic"]
            for d in deals
            if d["entry"] == DEAL_ENTRY_IN and d["magic"] != HAND
        }
        out: list[HandClose] = []
        for d in deals:
            reason = int(d.get("reason", -1))
            by_person = d["magic"] == HAND and reason in MANUAL_REASONS
            at_tp = reason == DEAL_REASON_TP  # HFM's take-profit, whatever magic it carries
            if (
                d["entry"] != DEAL_ENTRY_OUT
                or d.get("type") not in DEAL_TRADES
                or not (by_person or at_tp)
            ):
                continue
            m = opened_by.get(d["position_id"])
            coid = ids.by_magic.get(m) if m is not None else None
            if coid is None:
                continue  # not a position the system opened
            charges = _d(d.get("commission")) + _d(d.get("fee")) + _d(d.get("swap"))
            out.append(
                HandClose(
                    str(d["ticket"]),
                    coid,
                    _d(d["volume"]) * self._contract(snap, d["symbol"]),
                    _d(d["price"]),
                    max(-charges, ZERO),
                    self._utc(snap, d["time_msc"]),
                    "TAKE_PROFIT" if at_tp else "HAND",
                )
            )
        return out

    async def own_deals(self, account_ref: uuid.UUID, since: datetime) -> list[OwnDeal]:
        """The owner's deals by hand since ``since``, each with what it did to HFM's balance; the
        reconciliation books them outside the system's money (S59)."""
        snap, _ = await self._state()
        own, _ = by_hand(snap)
        out: list[OwnDeal] = []
        for d in snap.get("deals") or []:
            if d["ticket"] not in own:
                continue
            at = self._utc(snap, d["time_msc"])
            if at < since:
                continue
            result = sum(
                (_d(d.get(k)) for k in ("profit", "commission", "swap", "fee")), start=ZERO
            )
            out.append(OwnDeal(str(d["ticket"]), at, str(d.get("symbol") or ""), result))
        return sorted(out, key=lambda o: o.at)

    async def rates(
        self, symbol: str, timeframe: str, count: int, *, before: int | None = None
    ) -> list[list[Any]]:
        """HFM's own candles from MT5 (bridge version 4, read-only): [server time (s), open,
        high, low, close, tick volume], oldest first. The latest ``count``; or, with ``before``
        (HFM's server time in seconds, bridge version 6), the ``count`` up to that time."""
        snap, _ = await self._state()
        need = RATES_VERSION if before is None else HISTORY_VERSION
        if bridge_version(snap) < need:
            raise BrokerUnavailableError(
                f"the HFM bridge (version {snap.get('version')}) cannot send "
                f"{'candles' if before is None else 'older candles'}; update the bridge program "
                f"to version {need}"
            )
        payload: dict[str, Any] = {"symbol": symbol, "timeframe": timeframe, "count": count}
        if before is not None:
            payload["before"] = before
        res = await self._command("RATES", payload, mutating=False, keep=False)
        rows: list[list[Any]] = list(res.get("rates") or [])
        return rows

    async def take_profit_state(self, entry_client_order_id: str, price: Decimal) -> str:
        """Whether HFM holds this entry's take-profit (S83, S85), from HFM's own position record:
        - "HELD": every position of the entry carries exactly ``price``;
        - "TAKEN": HFM closed one at its take-profit (booked as the trade's exit);
        - "NONE": a position is open at HFM without it, or with another: an alarm (S85);
        - "PENDING": no position of the entry in the report (not reported yet, or just closed);
        - "UNSUPPORTED": a bridge before version 7 does not send it; the robot watches the
          target itself."""
        snap, _ = await self._state()
        if bridge_version(snap) < ATTACHED_TP_VERSION:
            return "UNSUPPORTED"
        m = magic(entry_client_order_id)
        deals = snap.get("deals") or []
        opened = {
            d["position_id"] for d in deals if d["magic"] == m and d["entry"] == DEAL_ENTRY_IN
        }
        if any(d["reason"] == DEAL_REASON_TP and d["position_id"] in opened for d in deals):
            return "TAKEN"
        held = [p for p in snap.get("positions") or [] if p["magic"] == m]
        if not held:
            return "PENDING"
        return "HELD" if all(_d(p.get("tp")) == price for p in held) else "NONE"

    async def unavailable_reason(self) -> str | None:
        """Why the bridge cannot be traded through now, in words; None when it can be read."""
        try:
            await self._state()
        except BrokerUnavailableError as exc:
            return str(exc)
        return None

    async def venue_status(self) -> str:
        try:
            snap, _ = await self._state()
        except BrokerUnavailableError:
            return "UNKNOWN"
        return "DEGRADED" if trading_off(snap) is not None else "OPERATIONAL"

    async def report(self) -> dict[str, Any]:
        """The bridge's latest report (fresh, demo account) — for the contract check."""
        snap, _ = await self._state()
        return snap

    async def account_fingerprint(self) -> str:
        snap, _ = await self._state()
        fp = (snap.get("account") or {}).get("fingerprint")
        if not fp:
            raise BrokerUnavailableError("the bridge did not report the account fingerprint")
        return str(fp)
