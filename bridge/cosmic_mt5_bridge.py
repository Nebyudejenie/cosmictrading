"""COSMICTRADING MetaTrader 5 bridge (ADR-028). Runs on Windows next to a logged-in MT5 terminal.

About once a second it:
1. reads the terminal (account mode, ticks, symbol rules, positions, orders, recent deals);
2. sends that snapshot, plus the results of finished commands, to ``POST /bridge/v1/sync``;
3. carries out the commands that come back, one by one, and reports them next time.

Rules it keeps whatever the server asks:
- Demo only: no trading request unless ``account_info().trade_mode`` is the DEMO mode.
- Buy only: a sell must name an open long position (``position=<ticket>``) and never exceeds
  its volume. A protective stop is the position's own ``sl``, so it can never open a short.
- Never twice: finished commands are journalled; every order carries ``magic`` derived from our
  client order ID and is looked up before it is sent and after any uncertain result.
- Never stale: a command past its deadline is not started.

Outbound HTTPS only. The HFM password stays in the terminal (``initialize`` uses its saved
login); the account number is never sent. Standard library plus the official ``MetaTrader5``
package. Facts it relies on: docs/integrations/mt5-python-api.md and hfm-mt5-demo.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

VERSION = "1"
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
    history_days: int = 7
    interval_seconds: float = 1.0

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
            history_days=int(raw.get("history_days", 7)),
            interval_seconds=float(raw.get("interval_seconds", 1.0)),
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

    def record(self, entry: dict[str, Any]) -> None:
        self.done[entry["command_id"]] = entry
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        self.unsent.append(entry)


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
        if a is not None:  # never the login number
            account = {
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
                    "currency_profit": s.currency_profit,
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
        """Snapshot → sync → run the returned commands. Returns commands run."""
        payload = self.snapshot()
        payload["results"] = list(self.journal.unsent)
        answer = self.post(payload)
        self.journal.unsent = [e for e in self.journal.unsent if e not in payload["results"]]
        ran = 0
        for command in answer.get("commands", []):
            self.journal.record(self.run_command(command))
            ran += 1
        return ran

    def run_command(self, command: dict[str, Any]) -> dict[str, Any]:
        cid = str(command["id"])
        if cid in self.journal.done:  # delivered twice: answer, never run again
            return self.journal.done[cid]
        now_ms = int(self.clock().timestamp() * 1000)
        try:
            if now_ms > int(command["expires_at_ms"]):
                return _failed(cid, "expired before it started; nothing was sent")
            kind = command["kind"]
            if kind == "PING":
                return _done(cid, {"demo": self.is_demo()})
            if not self.is_demo():
                return _failed(cid, "not a demo account; the bridge trades demo accounts only")
            payload = command["payload"]
            if kind == "SUBMIT":
                return _done(cid, self.submit(payload))
            if kind == "CANCEL":
                return _done(cid, self.cancel(payload))
            return _failed(cid, f"unknown command {kind}")
        except Exception as exc:  # report every failure; the server decides what it means
            log.exception("command %s failed", cid)
            return _failed(cid, f"{type(exc).__name__}: {exc}"[:400])

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

    def _positions(self, entry_coid: str, symbol: str) -> list[Any]:
        m = magic(entry_coid)
        return [
            p
            for p in self.mt5.positions_get(symbol=symbol) or ()
            if p.magic == m and p.type == self.mt5.POSITION_TYPE_BUY
        ]

    def _send(self, request: dict[str, Any], m: int) -> dict[str, Any]:
        result = self.mt5.order_send(request)
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
        status = "PLACED" if result.retcode == self.mt5.TRADE_RETCODE_PLACED else "FILLED"
        if result.retcode == self.mt5.TRADE_RETCODE_DONE_PARTIAL:
            status = "PARTIAL"
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
        if p["side"] == "BUY":
            if p["purpose"] != "ENTRY":
                return {"status": "REJECTED", "comment": "buys are entries only"}
            request: dict[str, Any] = {
                "symbol": symbol,
                "volume": float(volume),
                "magic": m,
                "comment": coid[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(symbol),
            }
            if Decimal(repr(tick.ask)) <= limit:
                request |= {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "type": mt5.ORDER_TYPE_BUY,
                    "price": tick.ask,
                    "deviation": 0,
                }
            elif p.get("time_in_force") == "IOC":
                return {"status": "EXPIRED", "comment": "ask above the limit (immediate or cancel)"}
            else:
                request |= {
                    "action": mt5.TRADE_ACTION_PENDING,
                    "type": mt5.ORDER_TYPE_BUY_LIMIT,
                    "price": float(limit),
                }
            return self._send(request, m)
        # SELL: protect or close an existing long; never opens a short.
        positions = self._positions(p["entry_client_order_id"], symbol)
        if not positions:
            return {"status": "REJECTED", "comment": "no open long position for this entry"}
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
        if Decimal(repr(tick.bid)) < limit:
            return {"status": "EXPIRED", "comment": "bid below the limit (immediate or cancel)"}
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
                    "type": mt5.ORDER_TYPE_SELL,
                    "position": pos.ticket,
                    "price": tick.bid,
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


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "bridge.log")],
    )
    import MetaTrader5 as mt5  # Windows only (PyPI ships win_amd64 wheels)

    config = Config.load(Path(argv[1]) if len(argv) > 1 else HERE / "config.json")
    if not config.url.startswith("https://"):
        log.error("the server URL must be https")
        return 2
    bridge = Bridge(mt5, config, https_post(config), Journal(HERE / "journal.jsonl"))
    log.info("bridge %s %s starting for %s", config.bridge, VERSION, ", ".join(config.symbols))
    connected = False
    while True:
        try:
            if not connected:
                connected = bridge.connect()
                if connected:
                    log.info("connected to the terminal; demo account: %s", bridge.is_demo())
            if connected:
                ran = bridge.cycle()
                if ran:
                    log.info("ran %d command(s)", ran)
        except urllib.error.URLError as exc:
            log.warning("server not reachable: %s", exc)
        except Exception:
            log.exception("cycle failed; reconnecting to the terminal")
            connected = False
            mt5.shutdown()
        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
