"""COSMICTRADING MetaTrader 5 bridge (ADR-028). Runs on Windows next to a logged-in MT5 terminal.

About once a second it:
1. reads the terminal (account mode, ticks, symbol rules, positions, orders, recent deals);
2. sends that snapshot, plus the results of finished commands, to ``POST /bridge/v1/sync``;
3. carries out the commands that come back, one by one, and reports them next time.

Rules it keeps whatever the server asks:
- Demo only: no trading request unless ``account_info().trade_mode`` is the DEMO mode.
- Buy only: a sell must name an open long position (``position=<ticket>``) and never exceeds
  its volume. A protective stop is the position's own ``sl``, so it can never open a short.
- Protected from the fill: an entry that carries its stop-loss (``attached_sl``, version 2)
  sends it with the buy, so HFM holds the stop on its server even if this computer goes offline.
- Never twice: finished commands are journalled; every order carries ``magic`` derived from our
  client order ID and is looked up before it is sent and after any uncertain result.
- Never stale: a command past its deadline is not started.
- Never late (version 8): an order that names how long it may still be sent (``ttl_ms``, counted
  from the moment it arrived here) is refused, unsent, once that has passed.
- Never on another account (version 7): an order names the account it is for
  (``expected_account``, a fingerprint) and is refused when MT5 is signed in to any other.
- Read-only terminal (version 7, ``"read_only": true`` in config.json): it only reports and
  signs in; it refuses every trading command, whatever the server sends (the REAL account).

The fast lane (version 8; the owner's rule of 2026-11-12: an order leaves for HFM within one
second of its prediction). The report above takes two to three seconds a round, measured, and
an order waited for the next one. So a second thread keeps one small request open to
``POST /bridge/v1/fast`` on a connection that stays up: it sends HFM's newest prices a few times
a second, and the server answers the moment an order waits. That order is sent to MT5 at once
and its result goes back on the next request. The lane starts only when the server offers it
(an older server never does), so this version works with either.

Three threads, and only one of them ever calls MT5 (the MetaTrader5 package is used from the
thread that connected, as before): the main thread reads the terminal and sends the orders; the
other two do nothing but wait on the network (the report's request, the fast lane's) and pass
what they are given through queues. So an order never waits for a request in flight, only for
the MT5 call in progress, and the account is still read as of one moment.

Outbound HTTPS only; the account number is never sent (only a one-way fingerprint). Since
version 7 the portal can sign the terminal in to an account the owner saved there (LOGIN): its
password arrives with that one command, is used once for ``mt5.login`` and is never written,
journaled or logged. An account already signed in is never signed in again (MT5 keeps its saved
login across restarts). Standard library plus the official ``MetaTrader5`` package. Facts it
relies on: docs/integrations/mt5-python-api.md and hfm-mt5-demo.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

RATE_TIMEFRAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
    "D1": "TIMEFRAME_D1",
}
MAX_RATES = 2000  # candles in one answer
VERSION = "8"  # 2: the stop-loss travels with the entry (attached_sl); 3: swap mode and day;
# 4: HFM's own candles on request (RATES), read-only; 5: a SELL entry opens a short (its stop
# above), and a stop, exit or take-profit acts on the entry's position, long or short; 6: RATES
# may read further back ("before"), so the server can store HFM's older candles page by page;
# 7: LOGIN and VERIFY (the portal's Broker Connections), the expected account on every order,
# the take-profit sent with the entry (attached_tp), a read-only terminal, tick values; 8: the
# fast lane (prices up and orders down on a connection kept open), an order's ``ttl_ms``, how
# long each step took in an order's result, and the report's own timings
HERE = Path(__file__).resolve().parent
log = logging.getLogger("cosmic_mt5_bridge")

# Results that mean "the request failed here and nothing was executed".
REJECTED_RETCODES = frozenset(
    {
        10004, 10006, 10007, 10013, 10014, 10015, 10016, 10017, 10018, 10019, 10020, 10021,
        10022, 10024, 10025, 10026, 10027, 10030, 10032, 10033, 10034, 10035, 10036, 10038,
        10039, 10040, 10042, 10043, 10044, 10045, 10046,
    }
)  # fmt: skip


NO_CHANGES = 10025  # TRADE_RETCODE_NO_CHANGES: the stop is already exactly that


def magic(client_order_id: str) -> int:
    """Same as cosmic.trading.brokers.mt5_bridge.magic: first 63 bits of SHA-256."""
    return int.from_bytes(hashlib.sha256(client_order_id.encode()).digest()[:8], "big") >> 1


def _num(value: Any) -> str | int | None:
    """MT5 floats travel as strings (exact repr); ints stay ints."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return repr(float(value))


def _row(item: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if hasattr(item, "_asdict"):  # MT5 returns named tuples
        data = item._asdict()
    elif isinstance(item, dict):
        data = item
    else:
        data = vars(item)
    out: dict[str, Any] = {}
    for f in fields:
        v = data.get(f)
        out[f] = v if isinstance(v, str) else _num(v)
    return out


POSITION_FIELDS = (
    "ticket", "time_msc", "type", "magic", "identifier", "volume", "price_open", "sl", "tp",
    "price_current", "swap", "profit", "symbol", "comment",
)  # fmt: skip
ORDER_FIELDS = (
    "ticket", "time_setup_msc", "time_done_msc", "type", "state", "magic", "position_id",
    "volume_initial", "volume_current", "price_open", "sl", "tp", "symbol", "comment",
)  # fmt: skip
DEAL_FIELDS = (
    "ticket", "order", "time_msc", "type", "entry", "magic", "position_id", "reason", "volume",
    "price", "commission", "swap", "profit", "fee", "symbol", "comment",
)  # fmt: skip


@dataclass
class Config:
    url: str
    bridge: str
    terminal: str
    symbols: tuple[str, ...]
    token: str
    cf_access: dict[str, str] = field(default_factory=dict)
    history_days: int = 120  # covers positions held for months (reconciliation needs the fills)
    interval_seconds: float = 1.0
    read_only: bool = False  # 7: a terminal that never trades (the REAL account's)
    fast_lane: bool = True  # 8: use the fast lane when the server offers it

    @staticmethod
    def load(path: Path) -> Config:
        raw = json.loads(path.read_text(encoding="utf-8"))
        token = (path.parent / raw["token_file"]).read_text(encoding="utf-8").strip()
        cf: dict[str, str] = {}
        if raw.get("cf_access_file"):
            cf = json.loads((path.parent / raw["cf_access_file"]).read_text(encoding="utf-8"))
        return Config(
            url=raw["url"].rstrip("/"),
            bridge=raw.get("bridge", "hfm-demo"),
            terminal=raw["terminal"],
            symbols=tuple(raw["symbols"]),
            token=token,
            cf_access=cf,
            history_days=int(raw.get("history_days", 120)),
            interval_seconds=float(raw.get("interval_seconds", 1.0)),
            read_only=bool(raw.get("read_only", False)),
            fast_lane=bool(raw.get("fast_lane", True)),
        )


class Journal:
    """Finished commands by ID (JSON lines), so a command delivered twice runs once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.done: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self.done[entry["command_id"]] = entry
        self.unsent: list[dict[str, Any]] = []
        self.fast: list[dict[str, Any]] = []  # 8: of ``unsent``, those the fast lane ran
        self.lock = threading.RLock()  # 8: the report and the fast lane both record and send

    def record(self, entry: dict[str, Any], *, keep: bool = True, fast: bool = False) -> None:
        """``keep``: journal it (a command that acts). A read-only answer (RATES: HFM's candles,
        thousands a day) is only sent; delivered twice, it is read again, harmlessly. ``fast``:
        the fast lane ran it, and sends its result itself (the report sends it too if it comes
        first; the server takes a result once)."""
        with self.lock:
            if keep:
                self.done[entry["command_id"]] = entry
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            self.unsent.append(entry)
            if fast:
                self.fast.append(entry)

    def waiting(self, *, fast: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.fast if fast else self.unsent)

    def delivered(self, entries: list[dict[str, Any]]) -> None:
        with self.lock:
            self.unsent = [e for e in self.unsent if e not in entries]
            self.fast = [e for e in self.fast if e not in entries]


class Bridge:
    def __init__(
        self,
        mt5: Any,
        config: Config,
        post: Callable[[dict[str, Any]], dict[str, Any]],
        journal: Journal,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.mt5 = mt5
        self.config = config
        self.post = post
        self.journal = journal
        self.clock = clock
        self.offset_ms: int | None = None  # broker server time minus UTC (last trustworthy)
        # 8: the two lanes, and the one thread that calls MT5 (``serve``). Commands arrive from
        # the network threads in ``inbox`` (the command, when it arrived, its lane) and are run
        # by the MT5 thread (``work``); a report is asked of it (``snapshot_wanted``) and handed
        # back (``snapshots``); the fast lane sends the prices it last read (``latest_ticks``).
        self.inbox: queue.Queue[tuple[dict[str, Any], float, str]] = queue.Queue()
        self.snapshot_wanted = threading.Event()
        self.snapshots: queue.Queue[tuple[dict[str, Any], float, float]] = queue.Queue(maxsize=1)
        self.latest_ticks: dict[str, Any] = {}
        self.between: Callable[[], Any] | None = None  # run between a snapshot's two readings
        self.wake = threading.Event()  # the fast lane ran an order: the next report goes at once
        self.fast_offer: dict[str, Any] | None = None  # what the server last offered, if anything
        self.fast_info: dict[str, Any] = {}  # this bridge's measurements, sent on the fast lane
        self.timing: dict[str, Any] = {}  # how long the last report took, sent with the next
        self._sent_at: tuple[float, float] | None = None  # the last order_send: start, end
        # 8: the order of things. With two lanes a command's result and the report that shows
        # what it did no longer travel together, so each is numbered: this run of the bridge
        # (when it started, in ms) and how many commands it has finished. A report says how many
        # were finished when it read the account; the server waits for a report that has reached
        # a command's own number before it reads that command's outcome from it.
        self.epoch_ms = int(time.time() * 1000)
        self.finished = 0

    def server_offset(self, ticks: dict[str, Any]) -> int | None:
        """MT5 stamps ticks, orders and deals in the broker's server time (observed UTC+3 at HFM
        on 2026-10-15). The freshest tick, compared with our UTC clock and rounded to whole 15
        minutes (time-zone steps), gives the offset; it is trusted only when the rest is under
        two minutes, so a stale tick never sets it."""
        if not ticks:
            return self.offset_ms
        now_ms = int(self.clock().timestamp() * 1000)
        raw = max(int(t["time_msc"]) for t in ticks.values()) - now_ms
        quarter = 900_000
        estimate = round(raw / quarter) * quarter
        if abs(raw - estimate) <= 120_000 and abs(estimate) <= 14 * 3_600_000:
            self.offset_ms = estimate
        return self.offset_ms

    # ---- terminal -----------------------------------------------------------------------

    def connect(self) -> bool:
        ok = bool(self.mt5.initialize(self.config.terminal, timeout=60000))
        if not ok:
            log.warning("terminal not reachable: %s", self.mt5.last_error())
        return ok

    def is_demo(self) -> bool:
        a = self.mt5.account_info()
        return a is not None and a.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO

    def fingerprint(self) -> str | None:
        a = self.mt5.account_info()
        return None if a is None else _fingerprint(a.login, a.server)

    def snapshot(self) -> dict[str, Any]:
        mt5 = self.mt5
        t = mt5.terminal_info()
        a = mt5.account_info()
        modes = {
            mt5.ACCOUNT_TRADE_MODE_DEMO: "DEMO",
            mt5.ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
            mt5.ACCOUNT_TRADE_MODE_REAL: "REAL",
        }
        margin = {
            mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING: "NETTING",
            mt5.ACCOUNT_MARGIN_MODE_EXCHANGE: "EXCHANGE",
            mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: "HEDGING",
        }
        terminal = {
            "connected": bool(t.connected) if t else False,
            "trade_allowed": bool(t.trade_allowed) if t else False,
            "tradeapi_disabled": bool(t.tradeapi_disabled) if t else True,
            "build": t.build if t else None,
        }
        account: dict[str, Any] = {}
        if a is not None:  # never the login number: only a one-way fingerprint of it
            account = {
                "fingerprint": _fingerprint(a.login, a.server),
                "trade_mode": modes.get(a.trade_mode, str(a.trade_mode)),
                "margin_mode": margin.get(a.margin_mode, str(a.margin_mode)),
                "currency": a.currency,
                "leverage": a.leverage,
                "balance": _num(a.balance),
                "equity": _num(a.equity),
                "margin": _num(a.margin),
                "margin_free": _num(a.margin_free),
                "server": a.server,
                "company": a.company,
                # 7: what HFM allows on this sign-in (False with the read-only investor
                # password), the margin level, and everything open on the account, any symbol
                "trade_allowed": bool(getattr(a, "trade_allowed", False)),
                "margin_level": _num(getattr(a, "margin_level", None)),
                "positions_total": int(mt5.positions_total() or 0),
                "orders_total": int(mt5.orders_total() or 0),
            }
        symbols: dict[str, Any] = {}
        ticks: dict[str, Any] = {}
        for name in self.config.symbols:
            mt5.symbol_select(name, True)
            s = mt5.symbol_info(name)
            if s is not None:
                symbols[name] = {
                    "contract_size": _num(s.trade_contract_size),
                    "volume_min": _num(s.volume_min),
                    "volume_step": _num(s.volume_step),
                    "volume_max": _num(s.volume_max),
                    "digits": s.digits,
                    "tick_size": _num(s.trade_tick_size),
                    "trade_mode": s.trade_mode,
                    "filling_mode": s.filling_mode,
                    "exemode": s.trade_exemode,
                    "stops_level": s.trade_stops_level,
                    "swap_long": _num(s.swap_long),
                    "swap_short": _num(s.swap_short),
                    # 3: what the swap numbers mean and when three nights are charged
                    "swap_mode": getattr(s, "swap_mode", None),
                    "swap_rollover3days": getattr(s, "swap_rollover3days", None),
                    "point": _num(getattr(s, "point", None)),
                    "currency_profit": s.currency_profit,
                    # 7: the money one tick moves for one lot, in the account's currency
                    "tick_value": _num(getattr(s, "trade_tick_value", None)),
                    "currency_margin": getattr(s, "currency_margin", None),
                }
            tick = mt5.symbol_info_tick(name)
            if tick is not None and tick.bid and tick.ask:
                ticks[name] = {
                    "bid": _num(tick.bid),
                    "ask": _num(tick.ask),
                    "time_msc": int(tick.time_msc),
                }
        wanted = set(self.config.symbols)
        now = self.clock()
        start = now - timedelta(days=self.config.history_days)
        end = now + timedelta(days=1)
        # 8: an order that arrived meanwhile is sent now, before the account is read, so that
        # it waits for half a snapshot at most. The account is then read as of one moment: no
        # command runs between these four readings (a position without its deal, or the
        # reverse, would be read as something gone wrong), and the report says how many
        # commands were finished at that moment.
        if self.between is not None:
            self.between()
        commands_done = [self.epoch_ms, self.finished]
        positions = [
            _row(p, POSITION_FIELDS) for p in (mt5.positions_get() or ()) if p.symbol in wanted
        ]
        orders = [_row(o, ORDER_FIELDS) for o in (mt5.orders_get() or ()) if o.symbol in wanted]
        history = [
            _row(o, ORDER_FIELDS)
            for o in (mt5.history_orders_get(start, end) or ())
            if o.symbol in wanted
        ]
        deals = [
            _row(d, DEAL_FIELDS)
            for d in (mt5.history_deals_get(start, end) or ())
            if d.symbol in wanted
        ]
        return {
            "commands_done": commands_done,
            "bridge": self.config.bridge,
            "version": VERSION,
            "sent_at_ms": int(now.timestamp() * 1000),
            "server_utc_offset_ms": self.server_offset(ticks),
            "terminal": terminal,
            "account": account,
            "symbols": symbols,
            "ticks": ticks,
            "positions": positions,
            "orders": orders,
            "history_orders": history,
            "deals": deals,
        }

    # ---- one cycle ----------------------------------------------------------------------

    def cycle(self) -> int:
        """Snapshot → sync → run the returned commands, in one thread. Returns commands run."""
        started = time.monotonic()
        payload = self.snapshot()
        commands, got = self._report(payload, started, time.monotonic(), self.post)
        for command in commands:
            entry = self.run_command(command, received=got, lane="report")
            self.journal.record(entry, keep=command.get("kind") != "RATES")
        return len(commands)

    def report_once(self, timeout: float = 30.0) -> int:
        """The report thread's round (version 8): ask the MT5 thread for a snapshot, send it,
        and pass the commands that come back to the MT5 thread. Returns commands passed on.
        ``queue.Empty`` when no snapshot came (the terminal is busy or not connected)."""
        self.snapshot_wanted.set()
        payload, started, snapped = self.snapshots.get(timeout=timeout)
        commands, got = self._report(payload, started, snapped, self.post)
        for command in commands:
            self.inbox.put((command, got, "report"))
        return len(commands)

    def _report(
        self,
        payload: dict[str, Any],
        started: float,
        snapped: float,
        post: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], float]:
        """Send one report with the results still to deliver; note what the server offers and
        how long it all took. Returns the commands it answered with, and when they arrived."""
        payload["results"] = self.journal.waiting()
        if self.fast_offer is None:  # a server before the fast lane does not know these fields
            payload.pop("commands_done", None)
        elif self.timing:
            payload["timing"] = dict(self.timing)
        answer = post(payload)
        got = time.monotonic()
        self.journal.delivered(payload["results"])
        offer = answer.get("fast")
        self.fast_offer = offer if isinstance(offer, dict) and self.config.fast_lane else None
        self.timing = {
            "snapshot_ms": round((snapped - started) * 1000),
            "post_ms": round((got - snapped) * 1000),
        }
        return list(answer.get("commands", [])), got

    def work(self, timeout: float = 0.0) -> int:
        """The MT5 thread's step (version 8): the commands the network threads passed on are
        run, in the order they arrived, and recorded. Waits up to ``timeout`` for the first, so
        an order is sent the moment it arrives. Returns commands run."""
        try:
            item = self.inbox.get(timeout=timeout) if timeout > 0 else self.inbox.get_nowait()
        except queue.Empty:
            return 0
        ran = 0
        while True:
            command, received, lane = item
            entry = self.run_command(command, received=received, lane=lane)
            fast = lane == "fast"
            self.journal.record(entry, keep=command.get("kind") != "RATES", fast=fast)
            ran += 1
            if fast:
                self.wake.set()  # the account changed: the report follows at once
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                return ran

    # ---- the fast lane (version 8) --------------------------------------------------------

    def ticks(self) -> dict[str, Any]:
        """HFM's newest price of each symbol: the fast lane's whole reading of the terminal."""
        out: dict[str, Any] = {}
        for name in self.config.symbols:
            tick = self.mt5.symbol_info_tick(name)
            if tick is not None and tick.bid and tick.ask:
                out[name] = {
                    "bid": _num(tick.bid),
                    "ask": _num(tick.ask),
                    "time_msc": int(tick.time_msc),
                }
        return out

    def fast_step(self, post: Callable[[dict[str, Any]], dict[str, Any]]) -> int:
        """One request on the fast lane, in one thread: prices and finished orders up; the
        orders that come back are sent to MT5 at once. Returns orders run."""
        payload = self.fast_request()
        started = time.monotonic()
        answer = post(payload)
        return self.fast_answer(payload, answer, started, time.monotonic())

    def fast_once(self, post: Callable[[dict[str, Any]], dict[str, Any]]) -> int:
        """The fast lane's own thread (version 8): the prices the MT5 thread last read go up;
        the orders that come back are passed to the MT5 thread, which sends them at once.
        Returns orders passed on."""
        payload = self.fast_request(self.latest_ticks)
        started = time.monotonic()
        answer = post(payload)
        got = time.monotonic()
        return self.fast_answer(
            payload, answer, started, got, pass_on=lambda c: self.inbox.put((c, got, "fast"))
        )

    def fast_request(self, ticks: dict[str, Any] | None = None) -> dict[str, Any]:
        """``ticks``: the prices to send (the MT5 thread's latest); read now when not given."""
        offer = self.fast_offer or {}
        results = self.journal.waiting(fast=True)
        return {
            "bridge": self.config.bridge,
            "version": VERSION,
            "sent_at_ms": int(self.clock().timestamp() * 1000),
            "server_utc_offset_ms": self.offset_ms,
            "ticks": self.ticks() if ticks is None else ticks,
            "results": results,
            # a finished order's result does not wait: the server answers at once
            "hold_ms": 0 if results else int(offer.get("hold_ms", 0)),
            "info": dict(self.fast_info),
        }

    def fast_answer(
        self,
        payload: dict[str, Any],
        answer: dict[str, Any],
        started: float,
        got: float,
        pass_on: Callable[[dict[str, Any]], Any] | None = None,
    ) -> int:
        """``started``, ``got``: when the request left and its answer arrived (monotonic).
        ``pass_on``: where the orders go (the MT5 thread's inbox); run here when not given."""
        self.journal.delivered(payload["results"])
        self._measure(int(payload["sent_at_ms"]), started, got, answer)
        commands = list(answer.get("commands", []))
        for command in commands:
            if pass_on is not None:
                pass_on(command)
                continue
            entry = self.run_command(command, received=got, lane="fast")
            self.journal.record(entry, keep=command.get("kind") != "RATES", fast=True)
        if commands and pass_on is None:
            self.wake.set()  # the account changed: the report follows at once
        return len(commands)

    def _measure(self, sent_ms: int, started: float, got: float, answer: dict[str, Any]) -> None:
        """This computer's clock against the server's, and the round trip, from one request:
        the server says when it received it and when it answered (so its hold is not counted)."""
        received, replied = answer.get("received_ms"), answer.get("replied_ms")
        if not isinstance(received, int) or not isinstance(replied, int):
            return
        back_ms = sent_ms + (got - started) * 1000  # when the answer arrived, on this clock
        rtt = (back_ms - sent_ms) - (replied - received)
        offset = ((received - sent_ms) + (replied - back_ms)) / 2
        self.fast_info = {
            "rtt_ms": round(rtt),
            "clock_offset_ms": round(offset),  # the server's clock minus this computer's
            **self.timing,
        }

    def run_command(
        self, command: dict[str, Any], *, received: float | None = None, lane: str = "report"
    ) -> dict[str, Any]:
        """``received``: when the command arrived here (``time.monotonic()``); ``lane``: the
        report or the fast lane. Called by the one thread that calls MT5."""
        arrived = time.monotonic() if received is None else received
        self._sent_at = None
        entry = self._run(command, arrived)
        fresh = "seq" not in (entry.get("result") or {})  # not one answered before
        if fresh:  # its number: reports read from now on show what it did
            self.finished += 1
            entry["result"] = {
                **(entry.get("result") or {}),
                "seq": [self.epoch_ms, self.finished],
            }
        if command.get("kind") == "SUBMIT" and entry.get("status") == "DONE" and fresh:
            # 8: how long each step took here, for the server's record of the order's speed
            done = time.monotonic()
            first = self._sent_at
            entry["result"] = {
                **entry["result"],
                "timing": {
                    "lane": lane,
                    "wait_ms": round(((first[0] if first else done) - arrived) * 1000),
                    "send_ms": round((first[1] - first[0]) * 1000) if first else None,
                    "total_ms": round((done - arrived) * 1000),
                },
            }
        return entry

    def _run(self, command: dict[str, Any], arrived: float) -> dict[str, Any]:
        cid = str(command["id"])
        with self.journal.lock:
            if cid in self.journal.done:  # delivered twice: answer, never run again
                return self.journal.done[cid]
        now_ms = int(self.clock().timestamp() * 1000)
        try:
            timed = (command.get("payload") or {}).get("ttl_ms") is not None
            # An order with its own time limit (8) is judged by that, counted here from its
            # arrival: its deadline is a second away, and two computers' clocks differ by more.
            if not timed and now_ms > int(command["expires_at_ms"]):
                return _failed(cid, "expired before it started; nothing was sent")
            kind = command["kind"]
            if kind == "PING":
                return _done(cid, {"demo": self.is_demo()})
            if kind == "VERIFY":  # 7: who is signed in, without signing in
                return _done(cid, self.identity(command.get("payload") or {}, "already signed in"))
            if kind == "LOGIN":  # 7: its own handling: the password never reaches a log
                return self.login_command(cid, command.get("payload") or {})
            if self.config.read_only:
                return _failed(cid, "this terminal is read-only; it never trades")
            if not self.is_demo():
                return _failed(cid, "not a demo account; the bridge trades demo accounts only")
            payload = command["payload"]
            want = payload.get("expected_account")
            if want is not None and self.fingerprint() != want:
                return _failed(cid, "MT5 is signed in to another account; nothing was sent")
            if kind == "SUBMIT":
                ttl = payload.get("ttl_ms")  # 8: how long it may still be sent, from arrival
                if ttl is not None and (time.monotonic() - arrived) * 1000 > float(ttl):
                    return _failed(cid, "late: its time to be sent had passed; nothing was sent")
                return _done(cid, self.submit(payload))
            if kind == "CANCEL":
                return _done(cid, self.cancel(payload))
            if kind == "RATES":
                return _done(cid, self.rates(payload))
            return _failed(cid, f"unknown command {kind}")
        except Exception as exc:  # report every failure; the server decides what it means
            log.exception("command %s failed", cid)
            return _failed(cid, f"{type(exc).__name__}: {exc}"[:400])

    # ---- the account itself (version 7): who is signed in, and signing in -----------------

    def identity(self, p: dict[str, Any], note: str) -> dict[str, Any]:
        """Who MT5 is signed in to, as HFM reports it (never the login number itself)."""
        mt5 = self.mt5
        a = mt5.account_info()
        t = mt5.terminal_info()
        if a is None:
            return {"ok": False, "code": "NOT_SIGNED_IN", "message": "MT5 is not signed in"}
        modes = {
            mt5.ACCOUNT_TRADE_MODE_DEMO: "DEMO",
            mt5.ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
            mt5.ACCOUNT_TRADE_MODE_REAL: "REAL",
        }
        fp = _fingerprint(a.login, a.server)
        want = p.get("expected_fingerprint")
        out = {
            "ok": want is None or fp == want,
            "fingerprint": fp,
            "trade_mode": modes.get(a.trade_mode, str(a.trade_mode)),
            "trade_allowed": bool(getattr(a, "trade_allowed", False)),
            "server": a.server,
            "company": a.company,
            "currency": a.currency,
            "leverage": a.leverage,
            "balance": _num(a.balance),
            "equity": _num(a.equity),
            "margin_free": _num(a.margin_free),
            "margin_level": _num(getattr(a, "margin_level", None)),
            "connected": bool(t.connected) if t else False,
            "note": note,
        }
        if not out["ok"]:
            out |= {"code": "OTHER_ACCOUNT", "message": "MT5 is signed in to another account"}
        return out

    def login_command(self, cid: str, p: dict[str, Any]) -> dict[str, Any]:
        password = str(p.get("password") or "")
        try:
            return _done(cid, self.login(p, password))
        except Exception as exc:  # never the password in what is reported or logged
            text = f"{type(exc).__name__}: {exc}".replace(password, "***") if password else ""
            log.warning("sign-in command %s failed: %s", cid, text[:200] or type(exc).__name__)
            return _failed(cid, (text or type(exc).__name__)[:400])

    def login(self, p: dict[str, Any], password: str) -> dict[str, Any]:
        """Sign MT5 in to the account the portal names. Already signed in to it: nothing is sent
        (MT5 keeps it signed in). Another account with anything open on it: refused unless the
        owner chose to leave it there (``allow_open``); MT5 never closes or moves positions when
        it changes account, they stay at HFM with their stop-loss. A refused sign-in signs the
        terminal back in to the account it had, with the password MT5 saved for it."""
        mt5 = self.mt5
        login, server = int(p["login"]), str(p["server"])
        before = mt5.account_info()
        if before is not None and before.login == login and before.server == server:
            return self.identity(p, "already signed in; the password was not needed")
        if before is not None and not p.get("allow_open"):
            busy = int(mt5.positions_total() or 0) + int(mt5.orders_total() or 0)
            if busy:
                return {
                    "ok": False,
                    "code": "OPEN_ON_CURRENT",
                    "message": f"the account signed in now has {busy} open position(s) or "
                    "order(s); nothing was changed",
                }
        if not password:
            return {"ok": False, "code": "NO_PASSWORD", "message": "no password was sent"}
        if not mt5.login(login, password=password, server=server, timeout=60000):
            code, text = mt5.last_error()
            restored = None
            if before is not None:
                restored = bool(mt5.login(before.login, server=before.server, timeout=60000))
            return {
                "ok": False,
                "code": str(code),
                "message": str(text).replace(password, "***")[:200],
                "restored": restored,
            }
        out = self.identity(p, "signed in")
        mode = p.get("expected_mode")  # what the portal expects: never trust the label alone
        if out.get("ok") and mode and out.get("trade_mode") != mode:
            restored = None
            if before is not None:  # back to the account it had, with MT5's saved password
                restored = bool(mt5.login(before.login, server=before.server, timeout=60000))
            out |= {
                "ok": False,
                "code": "WRONG_TYPE",
                "message": f"HFM reports a {out.get('trade_mode')} account, not {mode}",
                "restored": restored,
            }
        return out

    # ---- HFM's own candles (version 4), read-only -----------------------------------------

    def rates(self, p: dict[str, Any]) -> dict[str, Any]:
        """MT5's candles of one symbol, oldest first: [server time (s), open, high, low, close
        (as text), tick volume]. The latest ones; or, with ``before`` (version 6, HFM's server
        time in seconds), the ones up to that time, going back. Reads only; it trades nothing."""
        symbol, tf = str(p["symbol"]), str(p["timeframe"])
        count = max(1, min(int(p["count"]), MAX_RATES))
        if symbol not in self.config.symbols or tf not in RATE_TIMEFRAMES:
            return {"rates": [], "comment": "unknown symbol or timeframe"}
        self.mt5.symbol_select(symbol, True)
        frame = getattr(self.mt5, RATE_TIMEFRAMES[tf])
        before = p.get("before")
        raw = (
            self.mt5.copy_rates_from_pos(symbol, frame, 0, count)
            if before is None
            else self.mt5.copy_rates_from(symbol, frame, int(before), count)
        )
        rows = [] if raw is None else list(raw)
        return {
            "symbol": symbol,
            "timeframe": tf,
            "rates": [
                [  # prices as exact text: the server keeps money out of floats
                    int(r["time"]),
                    repr(float(r["open"])),
                    repr(float(r["high"])),
                    repr(float(r["low"])),
                    repr(float(r["close"])),
                    int(r["tick_volume"]),
                ]
                for r in rows
            ],
        }

    # ---- trading ------------------------------------------------------------------------

    def _filling(self, symbol: str) -> int:
        flags = int(self.mt5.symbol_info(symbol).filling_mode)
        if flags & 1:
            return int(self.mt5.ORDER_FILLING_FOK)
        if flags & 2:
            return int(self.mt5.ORDER_FILLING_IOC)
        return int(self.mt5.ORDER_FILLING_RETURN)

    def _existing(self, m: int) -> dict[str, Any] | None:
        """Our order found again by its magic number: pending, finished or dealt."""
        mt5 = self.mt5
        for o in mt5.orders_get() or ():
            if o.magic == m:
                return {"status": "PLACED", "order": o.ticket, "found": "open order"}
        now = self.clock()
        start, end = now - timedelta(days=self.config.history_days), now + timedelta(days=1)
        deals = [d for d in (mt5.history_deals_get(start, end) or ()) if d.magic == m]
        if deals:
            volume = sum(Decimal(repr(d.volume)) for d in deals)
            return {
                "status": "FILLED",
                "order": deals[0].order,
                "deals": [d.ticket for d in deals],
                "volume": str(volume),
                "found": "deals",
            }
        for o in mt5.history_orders_get(start, end) or ():
            if o.magic == m:
                return {"status": "ENDED", "order": o.ticket, "state": o.state, "found": "history"}
        return None

    def _stopped_out(self, entry_coid: str) -> list[int]:
        """This entry's positions that their stop-loss closed (deal reason SL)."""
        mt5, m = self.mt5, magic(entry_coid)
        now = self.clock()
        start, end = now - timedelta(days=self.config.history_days), now + timedelta(days=1)
        deals = list(mt5.history_deals_get(start, end) or ())
        opened = {d.position_id for d in deals if d.magic == m and d.entry == mt5.DEAL_ENTRY_IN}
        return sorted(
            {d.position_id for d in deals if d.reason == mt5.DEAL_REASON_SL}.intersection(opened)
        )

    def _positions(self, entry_coid: str, symbol: str) -> list[Any]:
        """The positions this entry opened: a long, or (since 5) a short."""
        m = magic(entry_coid)
        return [p for p in self.mt5.positions_get(symbol=symbol) or () if p.magic == m]

    def _enter(
        self, p: dict[str, Any], symbol: str, m: int, volume: Decimal, limit: Decimal, tick: Any
    ) -> dict[str, Any]:
        """A new position: a BUY at the ask at or below its limit, or a SELL (a short, since 5)
        at the bid at or above it; with an immediate-or-cancel order, never left resting. The
        stop-loss travels with it, held by HFM from the fill: below a long, above a short."""
        mt5, buy = self.mt5, p["side"] == "BUY"
        request: dict[str, Any] = {
            "symbol": symbol,
            "volume": float(volume),
            "magic": m,
            "comment": p["client_order_id"][:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(symbol),
        }
        if p.get("attached_sl") is not None:
            sl = Decimal(p["attached_sl"])
            if buy and not Decimal(0) < sl < limit:
                return {"status": "REJECTED", "comment": "the stop-loss must be below the entry"}
            if not buy and not sl > limit:
                return {"status": "REJECTED", "comment": "the stop-loss must be above the entry"}
            request["sl"] = float(sl)  # MT5 checks the distance from the price
        if p.get("attached_tp") is not None:  # 7: HFM holds the take-profit from the fill
            tp = Decimal(p["attached_tp"])
            if buy and not tp > limit:
                return {"status": "REJECTED", "comment": "the take-profit must be above the entry"}
            if not buy and not Decimal(0) < tp < limit:
                return {"status": "REJECTED", "comment": "the take-profit must be below the entry"}
            request["tp"] = float(tp)
        price = Decimal(repr(tick.ask if buy else tick.bid))
        if (buy and price <= limit) or (not buy and price >= limit):
            request |= {
                "action": mt5.TRADE_ACTION_DEAL,
                "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
                "price": tick.ask if buy else tick.bid,
                "deviation": 0,
            }
        elif p.get("time_in_force") == "IOC":
            side = "ask above" if buy else "bid below"
            return {"status": "EXPIRED", "comment": f"{side} the limit (immediate or cancel)"}
        else:
            request |= {
                "action": mt5.TRADE_ACTION_PENDING,
                "type": mt5.ORDER_TYPE_BUY_LIMIT if buy else mt5.ORDER_TYPE_SELL_LIMIT,
                "price": float(limit),
            }
        return self._send(request, m)

    def _send(self, request: dict[str, Any], m: int) -> dict[str, Any]:
        started = time.monotonic()
        result = self.mt5.order_send(request)
        if self._sent_at is None:  # the command's first order: when it left, and came back
            self._sent_at = (started, time.monotonic())
        if result is None or result.retcode not in (
            self.mt5.TRADE_RETCODE_DONE,
            self.mt5.TRADE_RETCODE_DONE_PARTIAL,
            self.mt5.TRADE_RETCODE_PLACED,
        ):
            code = None if result is None else result.retcode
            if code in REJECTED_RETCODES:
                return {"status": "REJECTED", "retcode": code, "comment": result.comment}
            found = self._existing(m)  # uncertain: did it happen anyway?
            if found is not None:
                return found
            return {"status": "UNKNOWN", "retcode": code, "error": str(self.mt5.last_error())}
        # What happened, not just the code: HFM's real server answers a placed BUY_LIMIT with
        # 10009 (DONE), deal 0 (2026-10-15). Only a deal is a fill.
        if request.get("action") == self.mt5.TRADE_ACTION_PENDING:
            status = "PLACED"
        elif result.retcode == self.mt5.TRADE_RETCODE_DONE_PARTIAL:
            status = "PARTIAL"
        elif result.deal:
            status = "FILLED"
        else:
            found = self._existing(m)
            return found if found is not None else {"status": "UNKNOWN", "retcode": result.retcode}
        return {
            "status": status,
            "retcode": result.retcode,
            "order": result.order,
            "deal": result.deal,
            "volume": _num(result.volume),
            "price": _num(result.price),
        }

    def submit(self, p: dict[str, Any]) -> dict[str, Any]:
        mt5 = self.mt5
        symbol, coid = p["symbol"], p["client_order_id"]
        if symbol not in self.config.symbols:
            return {"status": "REJECTED", "comment": f"{symbol} is not a configured symbol"}
        m = magic(coid)
        found = self._existing(m)
        if found is not None and p["purpose"] in ("ENTRY", "EXIT", "TAKE_PROFIT"):
            return {**found, "idempotent": True}
        volume = Decimal(p["volume"])
        limit = Decimal(p["limit_price"])
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or not tick.bid or not tick.ask:
            return {"status": "REJECTED", "comment": f"no current price for {symbol}"}
        if p["purpose"] == "ENTRY":
            return self._enter(p, symbol, m, volume, limit, tick)
        # A stop, an exit or a take-profit acts on this entry's own positions: a SELL on a long,
        # a BUY on a short (5); never a new position.
        positions = self._positions(p["entry_client_order_id"], symbol)
        long = p["side"] == "SELL"  # the side that closes a long is a sell
        want = mt5.POSITION_TYPE_BUY if long else mt5.POSITION_TYPE_SELL
        if any(pos.type != want for pos in positions):
            return {
                "status": "REJECTED",
                "comment": f"a {p['side'].lower()} cannot protect or close this position",
            }
        if not positions:
            entry = p["entry_client_order_id"]
            stopped = self._stopped_out(entry) if p["purpose"] == "STOP_LOSS" else []
            if stopped:
                # The stop sent with the entry closed the position before the server recorded
                # the stop order: report it, so its fill is booked instead of a failed stop.
                return {"status": "FILLED", "positions": stopped, "comment": "closed by its sl"}
            return {"status": "REJECTED", "comment": "no open position for this entry"}
        if p["purpose"] == "STOP_LOSS":
            stop = Decimal(p["stop_price"])
            done: list[int] = []
            for pos in positions:
                r = mt5.order_send(
                    {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "symbol": symbol,
                        "position": pos.ticket,
                        "sl": float(stop),
                        "tp": pos.tp,
                        "magic": m,
                    }
                )
                if r is None or r.retcode not in (mt5.TRADE_RETCODE_DONE, NO_CHANGES):
                    code = None if r is None else r.retcode
                    return {"status": "REJECTED", "retcode": code, "positions": done}
                done.append(pos.ticket)
            return {"status": "PLACED", "positions": done, "stop": str(stop)}
        if long and Decimal(repr(tick.bid)) < limit:
            return {"status": "EXPIRED", "comment": "bid below the limit (immediate or cancel)"}
        if not long and Decimal(repr(tick.ask)) > limit:
            return {"status": "EXPIRED", "comment": "ask above the limit (immediate or cancel)"}
        remaining = volume
        fills: list[dict[str, Any]] = []
        for pos in positions:
            if remaining <= 0:
                break
            lots = min(remaining, Decimal(repr(pos.volume)))
            out = self._send(
                {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": float(lots),
                    "type": mt5.ORDER_TYPE_SELL if long else mt5.ORDER_TYPE_BUY,
                    "position": pos.ticket,
                    "price": tick.bid if long else tick.ask,
                    "deviation": 0,
                    "magic": m,
                    "comment": coid[:31],
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": self._filling(symbol),
                },
                m,
            )
            if out["status"] not in ("FILLED", "PARTIAL"):
                return {**out, "fills": fills}
            fills.append(out)
            remaining -= Decimal(str(out.get("volume") or lots))
        return {"status": "FILLED" if remaining <= 0 else "PARTIAL", "fills": fills}

    def cancel(self, p: dict[str, Any]) -> dict[str, Any]:
        mt5 = self.mt5
        if p["purpose"] == "STOP_LOSS":
            done: list[int] = []
            for pos in self._positions(p["entry_client_order_id"], p["symbol"]):
                r = mt5.order_send(
                    {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "symbol": p["symbol"],
                        "position": pos.ticket,
                        "sl": 0.0,
                        "tp": pos.tp,
                    }
                )
                if r is None or r.retcode not in (mt5.TRADE_RETCODE_DONE, NO_CHANGES):
                    return {"status": "UNKNOWN", "retcode": None if r is None else r.retcode}
                done.append(pos.ticket)
            return {"status": "CANCELLED", "positions": done}
        m = magic(p["client_order_id"])
        for o in mt5.orders_get() or ():
            if o.magic == m:
                r = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
                    return {"status": "UNKNOWN", "retcode": None if r is None else r.retcode}
                return {"status": "CANCELLED", "order": o.ticket}
        return {"status": "NOT_OPEN"}


def _fingerprint(login: int, server: str) -> str:
    """Same as cosmic.trading.brokers.mt5_bridge.account_fingerprint."""
    return hashlib.sha256(f"{login}@{server}".encode()).hexdigest()[:16]


def _done(cid: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"command_id": cid, "status": "DONE", "result": result}


def _failed(cid: str, error: str) -> dict[str, Any]:
    return {"command_id": cid, "status": "FAILED", "result": {}, "error": error}


def https_post(config: Config) -> Callable[[dict[str, Any]], dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.token}",
        "User-Agent": f"cosmic-mt5-bridge/{VERSION}",
    }
    if config.cf_access:
        headers["CF-Access-Client-Id"] = config.cf_access["client_id"]
        headers["CF-Access-Client-Secret"] = config.cf_access["client_secret"]

    def post(payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(  # noqa: S310 (configured https URL, checked at start)
            f"{config.url}/bridge/v1/sync",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (configured https URL)
            answer: dict[str, Any] = json.loads(resp.read().decode())
            return answer

    return post


def https_fast(config: Config) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """POST to the fast lane on one connection that stays open (version 8): a new connection
    costs several round trips each time, a kept one a single one. Any failure closes it; the
    next request opens a new one."""
    url = urllib.parse.urlsplit(config.url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.token}",
        "User-Agent": f"cosmic-mt5-bridge/{VERSION}",
        "Connection": "keep-alive",
    }
    if config.cf_access:
        headers["CF-Access-Client-Id"] = config.cf_access["client_id"]
        headers["CF-Access-Client-Secret"] = config.cf_access["client_secret"]
    held: list[http.client.HTTPSConnection] = []
    gone = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)

    def post(payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        for attempt in (0, 1):
            kept = bool(held)
            if not kept:
                held.append(http.client.HTTPSConnection(url.netloc, timeout=15))
            conn = held[0]
            try:
                conn.request("POST", f"{url.path}/bridge/v1/fast", body, headers)
                resp = conn.getresponse()
                data = resp.read()
            except gone:
                # A kept connection the other side had closed: it answered nothing, so nothing
                # was taken from it. Once more, on a new connection.
                held.clear()
                conn.close()
                if kept and attempt == 0:
                    continue
                raise
            except Exception:
                held.clear()
                conn.close()
                raise
            if resp.status != 200:
                held.clear()
                conn.close()
                raise urllib.error.URLError(f"the fast lane answered {resp.status}")
            answer: dict[str, Any] = json.loads(data.decode())
            return answer
        raise urllib.error.URLError("the fast lane could not be reached")

    return post


FAST_BACKOFF = 2.0  # seconds without the lane after a failure, or while it is not offered
TICKS_EVERY = 0.05  # seconds: how often the MT5 thread reads HFM's newest prices for the lane
WORK_WAIT = 0.005  # seconds: how long the MT5 thread waits for an order before its other work


def fast_loop(
    bridge: Bridge,
    post: Callable[[dict[str, Any]], dict[str, Any]],
    ready: threading.Event,
    stop: threading.Event,
) -> None:
    """The fast lane's thread (version 8): request after request while the terminal is
    connected and the server offers the lane. It never calls MT5, and never ends on an error:
    it waits and goes on; the report carries every order meanwhile, as before version 8."""
    failing = False
    while not stop.is_set():
        if not ready.is_set() or bridge.fast_offer is None:
            stop.wait(FAST_BACKOFF)
            continue
        try:
            passed = bridge.fast_once(post)
            if passed:
                log.info("%d order(s) arrived on the fast lane", passed)
            if failing:
                log.info("the fast lane is back")
                failing = False
        except Exception as exc:
            if not failing:  # said once, not four times a second
                log.warning("the fast lane failed (%s); orders go with the report", exc)
                failing = True
            stop.wait(FAST_BACKOFF)


def report_loop(bridge: Bridge, ready: threading.Event, stop: threading.Event) -> None:
    """The report's thread (version 8): about once a second, and at once after an order on the
    fast lane, it asks the MT5 thread for a snapshot and sends it. It never calls MT5."""
    while not stop.is_set():
        if not ready.is_set():
            stop.wait(0.2)
            continue
        try:
            passed = bridge.report_once()
            if passed:
                log.info("%d command(s) arrived with the report", passed)
        except queue.Empty:
            log.warning("no snapshot from the terminal in time")
        except urllib.error.URLError as exc:
            log.warning("server not reachable: %s", exc)
        except Exception:
            log.exception("the report failed")
        bridge.wake.wait(bridge.config.interval_seconds)
        bridge.wake.clear()


def mt5_step(bridge: Bridge, last_ticks: float) -> float:
    """One round of the MT5 thread: the snapshot the report asked for, HFM's newest prices for
    the fast lane, and the commands that arrived (waited for, so an order is sent the moment
    it comes). Returns when the prices were last read."""
    if bridge.snapshot_wanted.is_set():
        started = time.monotonic()
        payload = bridge.snapshot()
        with contextlib.suppress(queue.Empty):  # one never taken (the report failed): replaced
            bridge.snapshots.get_nowait()
        bridge.snapshots.put((payload, started, time.monotonic()))
        # Cleared only now: a snapshot that failed is taken again after the terminal is back.
        bridge.snapshot_wanted.clear()
    now = time.monotonic()
    if bridge.fast_offer is not None and now - last_ticks >= TICKS_EVERY:
        bridge.latest_ticks = bridge.ticks()  # only while the lane runs
        last_ticks = now
    bridge.work(WORK_WAIT)
    return last_ticks


def serve(
    bridge: Bridge,
    post_fast: Callable[[dict[str, Any]], dict[str, Any]],
    stop: threading.Event,
) -> None:
    """Run the bridge (version 8) until ``stop``: this thread is the only one that calls MT5;
    the report and the fast lane each wait on the network in their own."""
    ready = threading.Event()  # the terminal is connected
    bridge.between = bridge.work  # an order that arrives during a snapshot is sent inside it
    threads = [
        threading.Thread(target=report_loop, args=(bridge, ready, stop), name="report"),
        threading.Thread(target=fast_loop, args=(bridge, post_fast, ready, stop), name="fast-lane"),
    ]
    for t in threads:
        t.daemon = True
        t.start()
    last_ticks = 0.0
    while not stop.is_set():
        try:
            if not ready.is_set():
                if not bridge.connect():
                    stop.wait(bridge.config.interval_seconds)
                    continue
                ready.set()
                log.info(
                    "connected to the terminal; demo account: %s%s",
                    bridge.is_demo(),
                    " (read-only terminal)" if bridge.config.read_only else "",
                )
            last_ticks = mt5_step(bridge, last_ticks)
        except Exception:
            log.exception("the terminal step failed; reconnecting to the terminal")
            ready.clear()
            bridge.mt5.shutdown()
            stop.wait(bridge.config.interval_seconds)
    for t in threads:
        t.join(timeout=20)


def single_instance(path: Path) -> Any:
    """Hold an exclusive lock on ``path`` for the life of the process: a second bridge on the
    same folder refuses to start (two bridges would run two code versions on one account)."""
    handle = path.open("a+")
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            raise SystemExit("another bridge is already running in this folder") from None
    return handle


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "bridge.log")],
    )
    import MetaTrader5 as mt5  # Windows only (PyPI ships win_amd64 wheels)

    lock = single_instance(HERE / "bridge.lock")  # held until the process ends

    config = Config.load(Path(argv[1]) if len(argv) > 1 else HERE / "config.json")
    if not config.url.startswith("https://"):
        log.error("the server URL must be https")
        return 2
    bridge = Bridge(mt5, config, https_post(config), Journal(HERE / "journal.jsonl"))
    log.info(
        "bridge %s %s starting for %s (lock %s)",
        config.bridge,
        VERSION,
        ", ".join(config.symbols),
        lock.name,
    )
    serve(bridge, https_fast(config), threading.Event())  # until the process is stopped
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
