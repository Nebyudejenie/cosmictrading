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

from sqlalchemy import desc, select

from cosmic.core.clock import Clock
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO, canonical_str
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_trading import bridge_commands, bridge_state
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
QUOTE_SOURCE = "hfm_mt5_bridge"
STATUS_SOURCE = "hfm_mt5_status"
QUOTE = "USD"

# Values exported by MetaTrader5 5.0.6231 (docs/integrations/hfm-mt5-demo.md).
DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
DEAL_REASON_SL = 4
ORDER_STATE_CANCELED, ORDER_STATE_FILLED, ORDER_STATE_REJECTED, ORDER_STATE_EXPIRED = 2, 4, 5, 6

STALE_AFTER = timedelta(seconds=15)
COMMAND_TTL = timedelta(seconds=20)
WAIT = timedelta(seconds=25)
ATTACHED_SL_VERSION = 2  # the first bridge that sends the stop-loss with the entry


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
    ) -> None:
        self.db = db
        self.clock = clock
        self.symbols = dict(symbols)  # instrument key → MT5 symbol
        self._instrument = {v: k for k, v in self.symbols.items()}
        self.bridge = bridge
        self.wait = wait
        self.poll_seconds = poll_seconds

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            name=ADAPTER,
            is_live=False,
            is_simulated=True,  # an MT5 demo account: virtual funds at the broker
            order_types=("LIMIT", "STOP_LIMIT"),
            time_in_force=("GTC", "IOC"),
            idempotent_client_ids=True,
            bracket_or_oco=False,
            attached_stop=True,  # the position's sl travels with the buy (bridge version 2)
            stops_per_position=True,  # a STOP_LOSS sets or clears the sl of the entry's positions
            partial_fills=True,
            data_freshness="the bridge's last report (at most 15 s old)",
            precision_source="symbol_info via the bridge (volume step × contract size)",
            rate_limits="one command at a time per bridge sync (about 1 s)",
            fees="commission, fee and swap per deal, as MT5 reports them (demo)",
            venue_status="terminal connected, trading allowed, demo account",
            reconciliation="magic number per order; deals and positions from the bridge",
        )

    # ---- the bridge's report -----------------------------------------------------------------

    async def _state(self) -> tuple[dict[str, Any], datetime]:
        async with self.db.connect() as conn:
            row = (
                await conn.execute(select(bridge_state).where(bridge_state.c.bridge == self.bridge))
            ).first()
        if row is None:
            raise BrokerUnavailableError("the HFM bridge has never connected")
        age = self.clock.now() - row.received_at
        if age > STALE_AFTER:
            raise BrokerUnavailableError(
                f"the HFM bridge last reported {int(age.total_seconds())} s ago"
            )
        snap: dict[str, Any] = row.snapshot
        if (snap.get("account") or {}).get("trade_mode") != "DEMO":
            raise BrokerUnavailableError("the MT5 terminal is not on a demo account")
        return snap, row.received_at

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

    async def _command(self, kind: str, payload: dict[str, Any], *, mutating: bool) -> Any:
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
                return row.result or {}
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
        res = await self._command(
            "CANCEL",
            {
                "client_order_id": client_order_id,
                "purpose": purpose,
                "symbol": symbol,
                "entry_client_order_id": entry,
            },
            mutating=True,
        )
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
        for o in snap.get("orders") or []:
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
            if _d(pos["sl"]) <= 0:
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
        entry_of_position = {
            d["position_id"]: by_magic.get(d["magic"]) for d in deals if d["entry"] == DEAL_ENTRY_IN
        }
        fills: list[VenueFill] = []
        for d in deals:
            if (
                d["entry"] not in (DEAL_ENTRY_IN, DEAL_ENTRY_OUT)
                or d["symbol"] not in self._instrument
            ):
                continue
            if d["reason"] == DEAL_REASON_SL:
                coid = stop_of.get(
                    entry_of_position.get(d["position_id"]) or "", f"mt5:{d['order']}"
                )
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
        for pos in snap.get("positions") or []:
            key = self._instrument.get(pos["symbol"])
            contract = self._contract(snap, pos["symbol"])
            qty = _d(pos["volume"]) * contract
            open_cost += qty * _d(pos["price_open"])
            base = key.split(":", 1)[1].split("/", 1)[0] if key else f"mt5:{pos['symbol']}"
            held[base] = held.get(base, ZERO) + qty
        cash = _d(account.get("balance")) - open_cost
        out = [VenueBalance(QUOTE, cash, cash)]
        out += [VenueBalance(asset, qty, qty) for asset, qty in sorted(held.items())]
        return out

    async def venue_status(self) -> str:
        try:
            snap, _ = await self._state()
        except BrokerUnavailableError:
            return "UNKNOWN"
        t = snap.get("terminal") or {}
        if t.get("connected") and t.get("trade_allowed") and not t.get("tradeapi_disabled"):
            return "OPERATIONAL"
        return "DEGRADED"

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
