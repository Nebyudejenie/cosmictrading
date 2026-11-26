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
- Its own demo account only (version 15, ``"only_account"`` in config.json, that account's
  fingerprint; the owner's choice of 2026-11-25, ADR-042): a second bridge on the real account's
  terminal tests the portal's orders on one Exness demo. It reports, and trades, only while
  that DEMO account is signed in; with any other account, the REAL one above all, it is silent
  and refuses every command, looking again at the very moment of every send. Only this
  computer's file can name the account: nothing the server sends changes it.

The fast lane (version 8; the owner's rule of 2026-11-12: an order leaves for HFM within one
second of its prediction). The report above takes two to three seconds a round, measured, and
an order waited for the next one. So a second thread keeps one small request open to
``POST /bridge/v1/fast`` on a connection that stays up: it sends HFM's newest prices a few times
a second, and the server answers the moment an order waits. That order is sent to MT5 at once
and its result goes back on the next request. The lane starts only when the server offers it
(an older server never does), so this version works with either.

Only one thread ever calls MT5 (the MetaTrader5 package is used from the thread that connected,
as before): the main thread reads the terminal and sends the orders; the others do nothing but
wait on the network (the report's request, the lanes') and pass what they are given through
queues. So an order never waits for a request in flight, only for the MT5 call in progress, and
the account is still read as of one moment.

Several lanes (version 9). The way to the server goes through the internet, and its round trip
varies from day to day (measured 2026-11-12 and 13: 0.06 s and 0.37 s). With one lane no
request waits at the server while an answer and the next request are on their way, so an order
arriving then waited up to a whole round trip. Now ``ORDER_LANES`` requests are held open for
``ORDER_HOLD_MS`` each, on their own connections, so that nearly always one is waiting; and a
price lane sends HFM's newest prices every ``PRICE_EVERY`` seconds without waiting, and takes an
order too when one waits.

The direct link (version 10, ``"direct"`` in config.json; only where this computer and the
server share a home network). The internet path's round trip was 0.36 to 0.43 s on 2026-11-13;
on the home network the server is 2 to 4 ms away. So the bridge keeps one SSH connection to the
server and sends the same requests through it: to a relay on the server's own loopback, which
passes them to the portal. Nothing new listens on the home network; the link's sign-in on the
server can forward to that relay and do nothing else; the server's key is pinned here; and the
bridge's token is required as on the internet path. While the link is down, or after a request
on it failed, every request goes over the internet path as before, with no gap: the link only
ever makes things faster, and the rules above (never late, never twice) are the same on both.

Outbound only: HTTPS to the portal and, when configured, that one SSH connection; the account
number is never sent (only a one-way fingerprint). Since
version 7 the portal can sign the terminal in to an account the owner saved there (LOGIN): its
password arrives with that one command, is used once for ``mt5.login`` and is never written,
journaled or logged. An account already signed in is never signed in again (MT5 keeps its saved
login across restarts). Standard library plus the official ``MetaTrader5`` package (and, for
the direct link, the OpenSSH client Windows ships). Facts it relies on:
docs/integrations/mt5-python-api.md and hfm-mt5-demo.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import logging.handlers
import queue
import re
import socket
import subprocess
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
VERSION = "16"  # 2: the stop-loss travels with the entry (attached_sl); 3: swap mode and day;
# 4: HFM's own candles on request (RATES), read-only; 5: a SELL entry opens a short (its stop
# above), and a stop, exit or take-profit acts on the entry's position, long or short; 6: RATES
# may read further back ("before"), so the server can store HFM's older candles page by page;
# 7: LOGIN and VERIFY (the portal's Broker Connections), the expected account on every order,
# the take-profit sent with the entry (attached_tp), a read-only terminal, tick values; 8: the
# fast lane (prices up and orders down on a connection kept open), an order's ``ttl_ms``, how
# long each step took in an order's result, and the report's own timings; 9: several lanes at
# once (order lanes held open for a second each, and a price lane), so an order nearly always
# finds a request already waiting at the server whatever the round trip is that day; 10: the
# direct link (an SSH connection to the server on the home network that carries the same
# requests in a few milliseconds), with the internet path as its fallback at every request;
# 11: that fallback takes over within about a second (an answer on the link is waited for one
# second beyond its hold, not two; over the internet four, not fifteen; a kept connection that
# lay unused is not trusted), after a night when it took 18 seconds; 12: a window that takes
# no lines (text selected in it) never stops the bridge: its lines wait in a queue and are
# dropped when it is full, and bridge.log gets every line; 13: the report says whether this
# terminal is read-only (the server shows the REAL account as connected only when it says so),
# and the broker's clock offset is measured only from a tick that has just arrived (a tick left
# from a closed market set it wrong whenever its age neared a whole quarter of an hour); it also
# reports what else HFM's terms say (freeze level, margin mode, spread, the stop-out level) and
# HFM's own margin and profit for one lot from MT5's calculators, so that nothing is assumed;
# a symbol configured as "XAUUSD*" is the account's own gold, whatever the broker calls it;
# 14: a closed MT5 program (its API answers nothing) is opened again by connecting to it anew,
# and a pattern prefers the account's own gold (XAUUSDm) to another product (XAUUSD247m)
# 15: a bridge may be bound to one DEMO account (``only_account``): silent, and refusing every
# command, while any other account is signed in on its terminal
# 16: a deal may fill as far from its price as its limit allows (``deviation``), so a broker on
# instant execution (Exness Pro) does not requote it whenever the price ticks
SYMBOLS_EVERY_S = 600.0  # 13: a symbol pattern ("XAUUSD*") is read again this often
MAX_EXPANDED = 5  # names one pattern may stand for
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


def _so_mode(mt5: Any, mode: Any) -> str | None:
    """MT5's ACCOUNT_STOPOUT_MODE: the stop-out and call levels in percent, or in money."""
    if mode is None:
        return None
    names = {
        getattr(mt5, "ACCOUNT_STOPOUT_MODE_PERCENT", 0): "PERCENT",
        getattr(mt5, "ACCOUNT_STOPOUT_MODE_MONEY", 1): "MONEY",
    }
    return names.get(mode, str(mode))


def _calculated(mt5: Any, symbol: str) -> dict[str, Any]:
    """HFM's own figures for one lot at the price now (13): the margin of a BUY and of a SELL,
    and the profit of a BUY when the price rises by 1.00, from MT5's order_calc_margin and
    order_calc_profit for this account. Nothing is sent; any failure leaves them out."""
    out: dict[str, Any] = {}
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or not tick.ask or not tick.bid:
            return out
        buy = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, symbol, 1.0, tick.ask)
        sell = mt5.order_calc_margin(mt5.ORDER_TYPE_SELL, symbol, 1.0, tick.bid)
        profit = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 1.0, tick.ask, tick.ask + 1.0)
    except Exception:  # an older terminal, or the calculators not available: not reported
        return out
    if buy is not None:
        out["margin_buy_1lot"] = _num(buy)
        out["margin_buy_at"] = _num(tick.ask)
    if sell is not None:
        out["margin_sell_1lot"] = _num(sell)
        out["margin_sell_at"] = _num(tick.bid)
    if profit is not None:
        out["profit_1lot_1"] = _num(profit)
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
class Direct:
    """10: the direct link's settings (``"direct"`` in config.json), for a bridge on the same
    home network as the server. Every value becomes an argument of the SSH client, so each is
    checked: none can be read as an option."""

    host: str  # the server's address on the home network
    user: str  # the link's own sign-in on the server: it can forward to the relay, nothing else
    key_file: Path  # this computer's private key for it (made here; it never leaves)
    known_hosts_file: Path  # the server's own key, pinned: any other server is refused
    port: int = 8745  # the relay's port on the server's loopback, and the tunnel's port here
    ssh: str = "ssh"  # the OpenSSH client

    @staticmethod
    def load(folder: Path, raw: dict[str, Any]) -> Direct:
        host, user, port = str(raw["host"]), str(raw["user"]), int(raw.get("port", 8745))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
            raise ValueError("direct.host must be a host name or an IPv4 address")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
            raise ValueError("direct.user must be a plain sign-in name")
        if not 1024 <= port <= 65535:
            raise ValueError("direct.port must be between 1024 and 65535")
        return Direct(
            host=host,
            user=user,
            key_file=folder / str(raw["key_file"]),
            known_hosts_file=folder / str(raw["known_hosts_file"]),
            port=port,
            ssh=str(raw.get("ssh", "ssh")),
        )


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
    # 15: the one DEMO account this bridge works with (its fingerprint); None: any account
    only_account: str | None = None
    fast_lane: bool = True  # 8: use the fast lane when the server offers it
    direct: Direct | None = None  # 10: the direct link, where there is one

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
            only_account=_only_account(raw.get("only_account")),
            fast_lane=bool(raw.get("fast_lane", True)),
            direct=Direct.load(path.parent, raw["direct"]) if raw.get("direct") else None,
        )


def _only_account(value: object) -> str | None:
    """15: ``only_account`` is an account fingerprint (16 lowercase hex characters) or absent;
    anything else stops the bridge at start rather than being guessed at."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{16}", value):
        raise ValueError("only_account must be an account fingerprint (16 hex characters)")
    return value


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


FAST_LANES = frozenset({"fast", "direct"})  # the lanes whose orders are run at once
INTERNET, DIRECT = "internet", "direct"  # 10: the two ways to the server


def _path(answer: dict[str, Any]) -> str:
    """10: the way an answer came (``two_ways`` writes it); the internet when not said."""
    return DIRECT if answer.get("via") == DIRECT else INTERNET


def _lane(answer: dict[str, Any]) -> str:
    """The lane named in an order's timing: the fast lane, or that lane on the direct link."""
    return DIRECT if _path(answer) == DIRECT else "fast"


def _plain_suffix(suffix: str) -> bool:
    """None, or only letters (an account type's mark: "m", "c"), never digits (another product)."""
    return suffix == "" or suffix.isalpha()


class TerminalGone(RuntimeError):
    """The MT5 program is not running (bridge 14): the bridge connects to it again."""


class Away(RuntimeError):
    """Another account than this bridge's own demo is signed in (bridge 15): it waits, silent."""


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
        self.offset_confirmed = False  # 13: measured from a tick that arrived while running
        self._newest_tick_ms: int | None = None  # 13: the freshest tick time seen last round
        self._symbols: tuple[str, ...] | None = None  # 13: the configured names, patterns expanded
        self._symbols_at = 0.0
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
        # 10: the same, by the path they were measured on ("direct", "internet"): a request
        # carries its own path's, so the server counts an order's way there on the right one
        self.path_info: dict[str, dict[str, Any]] = {}
        self.timing: dict[str, Any] = {}  # how long the last report took, sent with the next
        self._sent_at: tuple[float, float] | None = None  # the last order_send: start, end
        self._away = False  # 15: another account than its own demo was signed in (said once)
        # 8: the order of things. With two lanes a command's result and the report that shows
        # what it did no longer travel together, so each is numbered: this run of the bridge
        # (when it started, in ms) and how many commands it has finished. A report says how many
        # were finished when it read the account; the server waits for a report that has reached
        # a command's own number before it reads that command's outcome from it.
        self.epoch_ms = int(time.time() * 1000)
        self.finished = 0

    def symbols(self) -> tuple[str, ...]:
        """The symbols this bridge reads (13). A configured name ending in "*" stands for every
        symbol of the signed-in account starting with it, read again every 10 minutes: a broker's
        gold may be XAUUSD, XAUUSDm or XAUUSDc depending on the account type, and the name is
        then taken from the broker, never guessed. At most ``MAX_EXPANDED`` names a pattern."""
        if not any(name.endswith("*") for name in self.config.symbols):
            return self.config.symbols
        if self._symbols is not None and time.monotonic() - self._symbols_at < SYMBOLS_EVERY_S:
            return self._symbols
        out: list[str] = []
        for name in self.config.symbols:
            if not name.endswith("*"):
                out.append(name)
                continue
            try:
                found = self.mt5.symbols_get(group=name) or ()
            except Exception:  # a terminal without the call: the pattern reads nothing
                found = ()
            names = [str(s.name) for s in found]
            stem = name[:-1]
            # 14: the account's own gold first (only a letter after the stem: XAUUSDm,
            # XAUUSDc), shortest first, then any other (Exness also lists XAUUSD247m).
            names.sort(key=lambda n: (not _plain_suffix(n[len(stem) :]), len(n), n))
            out += names[:MAX_EXPANDED]
        self._symbols, self._symbols_at = tuple(dict.fromkeys(out)), time.monotonic()
        return self._symbols

    def server_offset(self, ticks: dict[str, Any]) -> int | None:
        """MT5 stamps ticks, orders and deals in the broker's server time (observed UTC+3 at HFM
        on 2026-10-15). The freshest tick, compared with our UTC clock and rounded to whole 15
        minutes (time-zone steps), gives the offset; it is trusted only when the rest is under
        two minutes and, after the first round, (13) only from a tick that arrived since the
        last one: a tick left from a closed market passes a whole quarter of an hour of age
        every 15 minutes, and set the offset wrong each time (terminal 2 read -14 hours on
        2026-11-21)."""
        if not ticks:
            return self.offset_ms
        now_ms = int(self.clock().timestamp() * 1000)
        newest = max(int(t["time_msc"]) for t in ticks.values())
        before, self._newest_tick_ms = self._newest_tick_ms, newest
        if before is not None and newest <= before:
            return self.offset_ms  # nothing new arrived: the price is as old as it was
        raw = newest - now_ms
        quarter = 900_000
        estimate = round(raw / quarter) * quarter
        if abs(raw - estimate) <= 120_000 and abs(estimate) <= 14 * 3_600_000:
            self.offset_ms = estimate
            # The first reading may be a closed market's last tick; one that arrived since is
            # proof the price is moving (13: the server trusts a broker without a published
            # clock rule, Exness, only then).
            self.offset_confirmed = self.offset_confirmed or before is not None
        return self.offset_ms

    # ---- terminal -----------------------------------------------------------------------

    def connect(self) -> bool:
        ok = bool(self.mt5.initialize(self.config.terminal, timeout=60000))
        if not ok:
            log.warning("terminal not reachable: %s", self.mt5.last_error())
            return False
        if not self.on_own_account():  # 15: not its own demo: it waits, silent
            if not self._away:
                log.info("another account is signed in; this bridge waits for its own demo")
                self._away = True
            return False
        self._away = False
        return True

    def on_own_account(self) -> bool:
        """15: a bridge bound to one account works only while that very DEMO account is
        signed in; one bound to none works with whatever account MT5 holds."""
        want = self.config.only_account
        if want is None:
            return True
        a = self.mt5.account_info()
        if a is None or a.trade_mode != self.mt5.ACCOUNT_TRADE_MODE_DEMO:
            return False
        return _fingerprint(a.login, a.server) == want

    def is_demo(self) -> bool:
        a = self.mt5.account_info()
        return a is not None and a.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO

    def fingerprint(self) -> str | None:
        a = self.mt5.account_info()
        return None if a is None else _fingerprint(a.login, a.server)

    def snapshot(self) -> dict[str, Any]:
        mt5 = self.mt5
        t = mt5.terminal_info()
        if t is None:
            # 14: the MT5 program was closed (its window, on 2026-11-24): its API answers
            # nothing, and every report would show an empty terminal for ever. Raised, so the
            # loop connects again, and MT5's initialize opens the terminal, which signs in with
            # its saved login. A terminal that is open but offline still reports (connected False).
            raise TerminalGone("MT5 is not running: connecting to it again opens it")
        if not self.on_own_account():  # 15: another account: nothing of it is reported
            raise Away("another account is signed in")
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
            "read_only": self.config.read_only,  # 13: it refuses every trading command
            "only_account": self.config.only_account is not None,  # 15: bound to one demo
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
                # 13: where HFM closes positions itself (the stop-out) and warns (the call),
                # in percent of margin level or in money, as margin_so_mode says
                "margin_so_call": _num(getattr(a, "margin_so_call", None)),
                "margin_so_so": _num(getattr(a, "margin_so_so", None)),
                "margin_so_mode": _so_mode(mt5, getattr(a, "margin_so_mode", None)),
                "positions_total": int(mt5.positions_total() or 0),
                "orders_total": int(mt5.orders_total() or 0),
            }
        symbols: dict[str, Any] = {}
        ticks: dict[str, Any] = {}
        for name in self.symbols():
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
                    # 13: what else HFM's terms say, read rather than assumed: the nearest a
                    # pending order or a stop may be changed (freeze level, in points), how the
                    # margin is computed, the spread now (in points), and HFM's own margin and
                    # profit for one lot (a 1.00 move), from MT5's own calculators
                    "freeze_level": getattr(s, "trade_freeze_level", None),
                    "calc_mode": getattr(s, "trade_calc_mode", None),
                    "margin_initial": _num(getattr(s, "margin_initial", None)),
                    "margin_maintenance": _num(getattr(s, "margin_maintenance", None)),
                    "spread_points": getattr(s, "spread", None),
                    "spread_float": getattr(s, "spread_float", None),
                    **_calculated(mt5, name),
                }
            tick = mt5.symbol_info_tick(name)
            if tick is not None and tick.bid and tick.ask:
                ticks[name] = {
                    "bid": _num(tick.bid),
                    "ask": _num(tick.ask),
                    "time_msc": int(tick.time_msc),
                }
        wanted = set(self.symbols())
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
            "server_utc_offset_confirmed": self.offset_confirmed,
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
            fast = lane in FAST_LANES
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
        for name in self.symbols():
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

    def fast_once(
        self, post: Callable[[dict[str, Any]], dict[str, Any]], hold_ms: int | None = None
    ) -> int:
        """A lane's own thread (version 8): the prices the MT5 thread last read go up; the
        orders that come back are passed to the MT5 thread, which sends them at once.
        ``hold_ms``: how long the server may hold this request (version 9; the server's own
        offer when not given). Returns orders passed on."""
        payload = self.fast_request(self.latest_ticks, hold_ms)
        started = time.monotonic()
        answer = post(payload)
        got = time.monotonic()
        lane = _lane(answer)
        return self.fast_answer(
            payload, answer, started, got, pass_on=lambda c: self.inbox.put((c, got, lane))
        )

    def fast_request(
        self, ticks: dict[str, Any] | None = None, hold_ms: int | None = None
    ) -> dict[str, Any]:
        """``ticks``: the prices to send (the MT5 thread's latest); read now when not given.
        ``hold_ms``: the lane's own hold; the server's offer when not given."""
        offer = self.fast_offer or {}
        if hold_ms is None:
            hold_ms = int(offer.get("hold_ms", 0))
        results = self.journal.waiting(fast=True)
        return {
            "bridge": self.config.bridge,
            "version": VERSION,
            "sent_at_ms": int(self.clock().timestamp() * 1000),
            "server_utc_offset_ms": self.offset_ms,
            "ticks": self.ticks() if ticks is None else ticks,
            "results": results,
            # a finished order's result does not wait: the server answers at once
            "hold_ms": 0 if results else hold_ms,
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
            entry = self.run_command(command, received=got, lane=_lane(answer))
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
        if answer.get("retried"):  # 10: sent twice (the link failed it first): not a measure
            return
        back_ms = sent_ms + (got - started) * 1000  # when the answer arrived, on this clock
        rtt = (back_ms - sent_ms) - (replied - received)
        offset = ((received - sent_ms) + (replied - back_ms)) / 2
        path = _path(answer)
        self.fast_info = {
            "rtt_ms": round(rtt),
            "clock_offset_ms": round(offset),  # the server's clock minus this computer's
            "path": path,  # 10: the way this was measured on
            **self.timing,
        }
        self.path_info[path] = self.fast_info

    def info_for(self, path: str) -> dict[str, Any]:
        """10: this bridge's measurements on ``path``, for a request about to travel on it; no
        round trip while none was measured there (the server then assumes its own)."""
        return dict(self.path_info.get(path) or {"path": path, **self.timing})

    def run_command(
        self, command: dict[str, Any], *, received: float | None = None, lane: str = "report"
    ) -> dict[str, Any]:
        """``received``: when the command arrived here (``time.monotonic()``); ``lane``: the
        report, the fast lane, or the fast lane on the direct link ("direct", version 10).
        Called by the one thread that calls MT5."""
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
            timing = {
                "lane": lane,
                "wait_ms": round(((first[0] if first else done) - arrived) * 1000),
                "send_ms": round((first[1] - first[0]) * 1000) if first else None,
                "total_ms": round((done - arrived) * 1000),
            }
            # 10: the round trip last measured on the path this order came by, so the server
            # counts its way here on that path and not on the other
            path = {"direct": DIRECT, "fast": INTERNET}.get(lane, "")
            rtt = (self.path_info.get(path) or {}).get("rtt_ms")
            if rtt is not None:
                timing["rtt_ms"] = rtt
            entry["result"] = {**entry["result"], "timing": timing}
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
            if not self.on_own_account():  # 15: its own demo only, whatever the command
                return _failed(
                    cid, "another account is signed in; this bridge works only with its own demo"
                )
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
        own = self.config.only_account
        if own is not None and _fingerprint(login, server) != own:
            # 15: a bridge bound to its demo never signs its terminal in to another account
            # (the real account's terminal above all): nothing is sent to MT5.
            return {
                "ok": False,
                "code": "NOT_ITS_ACCOUNT",
                "message": "this bridge signs in only its own demo account; nothing was changed",
            }
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
        if symbol not in self.symbols() or tf not in RATE_TIMEFRAMES:
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

    def _deviation(self, symbol: str, price: Decimal, limit: Decimal) -> int:
        """16: how far, in points, a deal may fill from the price it asks for: the room left
        to its limit, so it never fills worse than the limit. A broker on instant execution
        (Exness Pro, 2026-11-26) requotes a deal whose price moved further (10004); one on
        market execution (HFM) fills at the market anyway."""
        info = self.mt5.symbol_info(symbol)
        point = Decimal(repr(info.point)) if info is not None and info.point else Decimal(0)
        if point <= 0:
            return 0
        return max(int(abs(limit - price) / point), 0)

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
                "deviation": self._deviation(symbol, price, limit),  # 16: never past the limit
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

    def _order_send(self, request: dict[str, Any]) -> Any:
        """Every request to MT5's trade server goes through here (15): a read-only terminal
        never sends one, and a bridge bound to one demo looks again at who is signed in at the
        very moment, so an account changed by hand after the command was checked is never
        traded on."""
        if self.config.read_only or not self.on_own_account():
            raise PermissionError("not sent: read-only, or another account is signed in now")
        return self.mt5.order_send(request)

    def _send(self, request: dict[str, Any], m: int) -> dict[str, Any]:
        started = time.monotonic()
        result = self._order_send(request)
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
        if symbol not in self.symbols():
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
                r = self._order_send(
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
                    # 16: never past the exit's limit, on instant execution too
                    "deviation": self._deviation(
                        symbol, Decimal(repr(tick.bid if long else tick.ask)), limit
                    ),
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
                r = self._order_send(
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
                r = self._order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
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


KEPT_FRESH = 20.0  # seconds a kept connection may lie unused and still be used again (11)


def _kept(
    connect: Callable[[], http.client.HTTPConnection],
    path: str,
    headers: dict[str, str],
    what: str,
    patience: float,
    fresh: float = KEPT_FRESH,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """POST to ``path`` on one connection that stays open (version 8): a new connection costs
    several round trips each time, a kept one a single one. Any failure closes it; the next
    request opens a new one. ``connect``: a new connection, not yet opened.

    ``patience`` (version 11): how long an answer is waited for beyond the time the request
    lets the server hold it (``hold_ms``). ``fresh``: a kept connection unused for longer is
    not used again. Both from the night of 2026-11-17: while the direct link carried
    everything, the lanes' connections on the internet path lay unused for 19 hours; when the
    link failed for six seconds, each lane's request went out on such a connection, long
    dropped somewhere on the way, and waited 15 seconds for an answer that could not come."""
    held: list[http.client.HTTPConnection] = []
    used = [0.0]  # when the kept connection last carried a request (monotonic)
    gone = (
        http.client.RemoteDisconnected,
        ConnectionResetError,
        ConnectionAbortedError,  # 10: Windows says this of a loopback connection whose end died
        BrokenPipeError,
    )

    def post(payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        wait = int(payload.get("hold_ms") or 0) / 1000 + patience
        if held and time.monotonic() - used[0] > fresh:
            held.pop().close()  # unused for too long: nothing says it still leads anywhere
        for attempt in (0, 1):
            kept = bool(held)
            if not kept:
                held.append(connect())
            conn = held[0]
            conn.timeout = wait  # for the connection about to be opened
            if conn.sock is not None:
                conn.sock.settimeout(wait)  # and for the one already open
            used[0] = time.monotonic()
            try:
                conn.request("POST", path, body, headers)
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
                raise urllib.error.URLError(f"{what} answered {resp.status}")
            answer: dict[str, Any] = json.loads(data.decode())
            return answer
        raise urllib.error.URLError(f"{what} could not be reached")

    return post


def https_fast(config: Config) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The fast lane over the internet path (version 8), on its own kept connection."""
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
    return _kept(
        lambda: http.client.HTTPSConnection(url.netloc),
        f"{url.path}/bridge/v1/fast",
        headers,
        "the fast lane",
        INTERNET_PATIENCE,
    )


# ---- the direct link (version 10) ---------------------------------------------------------------

LINK_RETRY = 5.0  # seconds the internet path carries everything after a request on the link failed
LINK_RESTART = 5.0  # seconds before the tunnel is started again after it ended
LINK_RESTART_MAX = 300.0  # and at most, while it never comes up (each try twice as late)
LINK_START = 15.0  # seconds a starting tunnel is given to come up
# How long an answer is waited for beyond the request's own hold (version 11; ``_kept``). On
# the link the server is a few milliseconds away: a second without an answer means the link is
# not carrying, and the request goes over the internet path. (It was 2 s, and over the
# internet 15 s whatever the hold.)
DIRECT_PATIENCE = 1.0  # a lane's request on the link
DIRECT_REPORT_PATIENCE = 10.0  # the report's (the whole account; the server stores it first)


def _free_port() -> int:
    """A port on this computer's loopback that nothing uses now (the tunnel's own end)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


_JOBS: list[Any] = []  # the job objects of ``_tie``, kept for the life of this process


def _tie(process: Any) -> None:
    """Windows: the tunnel ends with this process, however this process ends (a job object
    that kills what it holds when its last handle closes). Without it a bridge stopped by
    force would leave its tunnel running. Never able to stop the bridge: said and gone on."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class Extended(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", Basic),
            ("IoInfo", ctypes.c_ulonglong * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        job = k32.CreateJobObjectW(None, None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # 9: JobObjectExtendedLimitInformation
        ok = job and k32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        ok = ok and k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle)))
        if not ok:
            raise OSError(ctypes.get_last_error(), "the job object was refused")
        _JOBS.append(job)
    except Exception as exc:
        log.warning("the tunnel is not tied to this process (%s); it is ended at a clean stop", exc)


class Link:
    """The direct link's tunnel (version 10): one ``ssh`` process that forwards a port on this
    computer's loopback to the relay on the server's, started again whenever it ends. The
    lanes ask ``port()`` before each request: a number while the tunnel is up and no request
    on it failed in the last ``LINK_RETRY`` seconds, otherwise None (the internet path)."""

    def __init__(
        self,
        direct: Direct,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        free_port: Callable[[], int] = _free_port,
        listening: Callable[[int], bool] = _listening,
    ) -> None:
        self.direct = direct
        self.popen = popen
        self.free_port = free_port
        self.listening = listening
        self.lock = threading.Lock()
        self._port: int | None = None
        self._down_until = 0.0
        self._failing = False

    def port(self) -> int | None:
        with self.lock:
            return self._port if time.monotonic() >= self._down_until else None

    def failed(self, exc: BaseException) -> None:
        """A request on the link failed: the internet path for ``LINK_RETRY`` seconds."""
        with self.lock:
            said, self._failing = self._failing, True
            self._down_until = time.monotonic() + LINK_RETRY
        if not said:  # once, not at every try
            log.warning("the direct link failed (%s); the internet path carries on", exc)

    def worked(self) -> None:
        with self.lock:
            said, self._failing = self._failing, False
        if said:
            log.info("the direct link is back")

    def command(self, port: int) -> list[str]:
        """The tunnel: no command, no terminal, nothing read from any other configuration; the
        server must show the pinned key; it ends by itself when the server stops answering.
        No ``ConnectTimeout``: with ``ServerAliveInterval`` beside it, the SSH client of the
        trading computer (OpenSSH for Windows 7.7) waited out that whole time at every start
        (measured 2026-11-14: 5.1 s, against 0.1 s without it). A start that hangs is ended by
        this thread after ``LINK_START`` seconds instead."""
        d = self.direct
        return [
            d.ssh, "-F", "none", "-N", "-T",
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={d.known_hosts_file}",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2",
            "-o", "LogLevel=ERROR",
            "-i", str(d.key_file),
            "-L", f"127.0.0.1:{port}:127.0.0.1:{d.port}",
            f"{d.user}@{d.host}",
        ]  # fmt: skip

    def run(self, stop: threading.Event) -> None:
        """The link's own thread: the tunnel, again and again, until ``stop``. Never ends on an
        error: the lanes use the internet path whenever ``port()`` gives none."""
        wait = LINK_RESTART
        while not stop.is_set():
            came_up = False
            try:
                came_up = self._once(stop)
            except Exception as exc:
                log.warning("the direct link could not be started (%s)", exc)
            with self.lock:
                self._port = None
            # A tunnel that never comes up (the server refuses the sign-in, say) is tried
            # ever more rarely: a server counts refused sign-ins against the computer they
            # come from.
            wait = LINK_RESTART if came_up else min(wait * 2, LINK_RESTART_MAX)
            stop.wait(wait)

    def _once(self, stop: threading.Event) -> bool:
        """One tunnel, from its start to its end. Returns whether it came up."""
        port = self.free_port()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = self.popen(
            self.command(port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
        _tie(process)
        # What the tunnel says is read as it comes (a pipe nobody reads fills up and would
        # stop the tunnel); its last line is kept for the log.
        said: list[bytes] = []

        def listen() -> None:
            for line in process.stderr:
                said[:] = [line]

        ear = threading.Thread(target=listen, name="direct-link-said", daemon=True)
        ear.start()
        try:
            started = time.monotonic()
            up = False
            while not stop.is_set() and process.poll() is None:
                if not up and self.listening(port):
                    up = True
                    with self.lock:
                        self._port = port
                    log.info("the direct link is up (to %s)", self.direct.host)
                if not up and time.monotonic() - started > LINK_START:
                    log.warning("the direct link did not come up in %.0f s", LINK_START)
                    return False
                stop.wait(0.05 if not up else 0.5)
            if process.poll() is not None:
                ear.join(timeout=1)
                last = said[0].decode(errors="replace").strip()[:200] if said else ""
                log.warning(
                    "the direct link ended (%s)%s", process.returncode, f": {last}" if last else ""
                )
            return up
        finally:
            with self.lock:
                self._port = None
            if process.poll() is None:
                process.terminate()
                with contextlib.suppress(Exception):
                    process.wait(timeout=5)


class _Whole(http.client.HTTPConnection):
    """A connection whose requests leave in one piece. ``http.client`` writes a request's head
    and its body one after the other; through a tunnel the second piece can wait for the first
    to be acknowledged (tens of milliseconds, on a way that takes two). Here the head is held
    until the body comes, and both are written together."""

    _head = b""

    def send(self, data: Any) -> None:
        if not self._head and isinstance(data, bytes) and data.endswith(b"\r\n\r\n"):
            self._head = data
            return
        head, self._head = self._head, b""
        super().send(head + data if head else data)

    def getresponse(self) -> http.client.HTTPResponse:
        if self._head:  # a request without a body: its head alone
            head, self._head = self._head, b""
            super().send(head)
        return super().getresponse()


def direct_post(
    config: Config, link: Link, path: str, patience: float
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """POST to ``path`` through the tunnel, on a kept connection of its own. Plain HTTP inside
    the SSH connection, to the relay on the server's loopback: the bridge's token as on the
    internet path, and none of the internet path's other credentials."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.token}",
        "User-Agent": f"cosmic-mt5-bridge/{VERSION}",
        "Connection": "keep-alive",
        "Host": "127.0.0.1",
    }

    def connect() -> http.client.HTTPConnection:
        port = link.port()
        if port is None:
            raise urllib.error.URLError("the direct link is down")
        return _Whole("127.0.0.1", port)

    return _kept(connect, path, headers, "the direct link", patience)


def two_ways(
    link: Link,
    direct: Callable[[dict[str, Any]], dict[str, Any]],
    internet: Callable[[dict[str, Any]], dict[str, Any]],
    info: Callable[[str], dict[str, Any]] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """One request: on the direct link while it is up, and over the internet path the moment
    it is not, or the request on it failed. The same request may so arrive twice; the server
    takes a result or a report twice without harm, and an order handed to an answer that never
    arrived is not sent by anyone (it ends unsent at its time limit, as on one path).
    The answer says which way it came (``via``) and whether the link failed it first
    (``retried``). ``info``: the bridge's measurements by path, put in a lane's request."""

    def on(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "info": info(path)} if info is not None else payload

    def post(payload: dict[str, Any]) -> dict[str, Any]:
        retried = False
        if link.port() is not None:
            try:
                answer = direct(on(DIRECT, payload))
            except Exception as exc:
                link.failed(exc)
                retried = True
            else:
                link.worked()
                answer["via"] = DIRECT
                return answer
        answer = internet(on(INTERNET, payload))
        answer["via"] = INTERNET
        if retried:
            answer["retried"] = True
        return answer

    return post


FAST_BACKOFF = 2.0  # seconds without the lane after a failure, or while it is not offered
# 11: how long a lane's answer over the internet path is waited for beyond the request's hold:
# a round trip of up to half a second on a bad day, a new connection's several round trips,
# and room. (15 s whatever the hold, before.)
INTERNET_PATIENCE = 4.0
ORDER_LANES = 3  # 9: requests held open at the server at once, each on its own connection
ORDER_HOLD_MS = 1000  # 9: how long the server may hold each of them (its own limit)
PRICE_EVERY = 0.25  # 9: seconds between the price lane's requests (it is never held)
TICKS_EVERY = 0.05  # seconds: how often the MT5 thread reads HFM's newest prices for the lane
WORK_WAIT = 0.005  # seconds: how long the MT5 thread waits for an order before its other work


def fast_loop(
    bridge: Bridge,
    post: Callable[[dict[str, Any]], dict[str, Any]],
    ready: threading.Event,
    stop: threading.Event,
    hold_ms: int | None = None,
    every: float = 0.0,
    after: float = 0.0,
) -> None:
    """One lane's thread (version 8): request after request while the terminal is connected
    and the server offers the lanes. It never calls MT5, and never ends on an error: it waits
    and goes on; the other lanes and the report carry every order meanwhile.
    ``hold_ms``: how long the server may hold each request (an order lane); 0 for the price
    lane, which then asks no more often than ``every`` seconds. ``after``: a first wait, so the
    lanes do not all ask at the same moment."""
    failing = False
    stop.wait(after)
    while not stop.is_set():
        if not ready.is_set() or bridge.fast_offer is None:
            stop.wait(FAST_BACKOFF)
            continue
        began = time.monotonic()
        try:
            passed = bridge.fast_once(post, hold_ms)
            if passed:
                log.info("%d order(s) arrived on a fast lane", passed)
            if failing:
                log.info("the fast lane is back")
                failing = False
        except Exception as exc:
            if not failing:  # said once, not several times a second
                log.warning("a fast lane failed (%s); the others and the report go on", exc)
                failing = True
            stop.wait(FAST_BACKOFF)
            continue
        left = every - (time.monotonic() - began)
        if left > 0:
            stop.wait(left)


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
    new_lane: Callable[[], Callable[[dict[str, Any]], dict[str, Any]]],
    stop: threading.Event,
    link: Link | None = None,
) -> None:
    """Run the bridge until ``stop``: this thread is the only one that calls MT5; the report
    and each lane wait on the network in their own. ``new_lane``: a lane's own way to the
    server (each call a new connection). ``link``: the direct link, kept up in its own thread
    (version 10)."""
    ready = threading.Event()  # the terminal is connected
    bridge.between = bridge.work  # an order that arrives during a snapshot is sent inside it
    threads = [threading.Thread(target=report_loop, args=(bridge, ready, stop), name="report")]
    if link is not None:
        threads.append(threading.Thread(target=link.run, args=(stop,), name="direct-link"))
    spread = ORDER_HOLD_MS / 1000 / ORDER_LANES  # the order lanes start a third of a hold apart
    for n in range(ORDER_LANES):
        threads.append(
            threading.Thread(
                target=fast_loop,
                args=(bridge, new_lane(), ready, stop, ORDER_HOLD_MS, 0.0, n * spread),
                name=f"order-lane-{n + 1}",
            )
        )
    threads.append(
        threading.Thread(
            target=fast_loop,
            args=(bridge, new_lane(), ready, stop, 0, PRICE_EVERY),
            name="price-lane",
        )
    )
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
                    " (read-only terminal)"
                    if bridge.config.read_only
                    else (" (its own demo account only)" if bridge.config.only_account else ""),
                )
            last_ticks = mt5_step(bridge, last_ticks)
        except Away:  # 15: said once by connect, not as a failure
            ready.clear()
            stop.wait(bridge.config.interval_seconds)
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


WINDOW_LINES = 500  # 12: lines waiting for the bridge's window before further ones are dropped


class _ToWindow(logging.handlers.QueueHandler):
    """12: lines for the bridge's window go through a queue, written by a thread of their own,
    so that a window that takes no lines never stops the bridge. On 2026-11-17 the window was
    held from 05:19 UTC (Windows holds a console program that writes while text is selected
    in its window); every thread of the bridge that then wrote a line stopped with it: the
    lanes at once, the report at 05:59, and the bridge was down until it was restarted. Now
    the window may be held for as long as it likes: its lines wait, then are dropped, and
    bridge.log still gets every line."""

    def __init__(self, lines: queue.Queue[logging.LogRecord], to_file: logging.Handler) -> None:
        super().__init__(lines)
        self.to_file = to_file
        self.dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1:  # said once, in the file only (the window takes nothing)
                note = logging.LogRecord(
                    "cosmic_mt5_bridge", logging.WARNING, __file__, 0,
                    "the bridge's window takes no lines (is text selected in it? press Esc "
                    "there); the bridge goes on, and its lines go on to bridge.log",
                    None, None,
                )  # fmt: skip
                self.to_file.handle(note)

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record  # the window's own handler formats it, as before


def logging_to(folder: Path, window: Any = None) -> logging.handlers.QueueListener:
    """12: every line to ``bridge.log`` at once, and to the window (``window``: a stream; the
    console when not given) by way of ``_ToWindow``. Returns the window's writer, started."""
    shape = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    to_file = logging.FileHandler(folder / "bridge.log")
    to_file.setFormatter(shape)
    on_screen = logging.StreamHandler(window)
    on_screen.setFormatter(shape)
    lines: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=WINDOW_LINES)
    writer = logging.handlers.QueueListener(lines, on_screen)
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.setLevel(logging.INFO)
    root.addHandler(to_file)
    root.addHandler(_ToWindow(lines, to_file))
    # A handler that fails says so on the console, which may be held too: not here.
    logging.raiseExceptions = False
    writer.start()
    return writer


def main(argv: list[str]) -> int:
    logging_to(HERE)
    import MetaTrader5 as mt5  # Windows only (PyPI ships win_amd64 wheels)

    lock = single_instance(HERE / "bridge.lock")  # held until the process ends

    config = Config.load(Path(argv[1]) if len(argv) > 1 else HERE / "config.json")
    if not config.url.startswith("https://"):
        log.error("the server URL must be https")
        return 2
    link = Link(config.direct) if config.direct is not None else None
    report = https_post(config)
    if link is not None:  # 10: the report too goes on the direct link while it is up
        report = two_ways(
            link, direct_post(config, link, "/bridge/v1/sync", DIRECT_REPORT_PATIENCE), report
        )
    bridge = Bridge(mt5, config, report, Journal(HERE / "journal.jsonl"))

    def new_lane() -> Callable[[dict[str, Any]], dict[str, Any]]:
        internet = https_fast(config)
        if link is None:
            return internet
        direct = direct_post(config, link, "/bridge/v1/fast", DIRECT_PATIENCE)
        return two_ways(link, direct, internet, bridge.info_for)

    log.info(
        "bridge %s %s starting for %s (lock %s)%s",
        config.bridge,
        VERSION,
        ", ".join(config.symbols),
        lock.name,
        f", with the direct link to {config.direct.host}" if config.direct is not None else "",
    )
    serve(bridge, new_lane, threading.Event(), link)  # until the process stops
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
