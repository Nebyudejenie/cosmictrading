"""The Windows MT5 bridge against the in-process MT5 emulator (ADR-028)."""

from __future__ import annotations

import http.client
import http.server
import json
import socket
import socketserver
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bridge import cosmic_mt5_bridge as program
from bridge.cosmic_mt5_bridge import VERSION as BRIDGE_VERSION
from bridge.cosmic_mt5_bridge import (
    Bridge,
    Config,
    Direct,
    Journal,
    Link,
    direct_post,
    magic,
    serve,
    two_ways,
)

from cosmic.trading.brokers.mt5_bridge import magic as server_magic
from cosmic.trading.brokers.mt5_bridge import reflects
from tests.mt5_emulator import ACCOUNT_TRADE_MODE_REAL, MT5Emulator

ENTRY = "ct-T-20261015-AB12-E"


def make(tmp_path: Path, emu: MT5Emulator | None = None) -> tuple[Bridge, MT5Emulator, list[Any]]:
    emu = emu or MT5Emulator()
    posted: list[dict[str, Any]] = []
    queue: list[list[dict[str, Any]]] = []

    def post(payload: dict[str, Any]) -> dict[str, Any]:
        posted.append(json.loads(json.dumps(payload)))  # what goes over the wire
        return {"commands": queue.pop(0) if queue else []}

    cfg = Config(
        "https://example.invalid", "hfm-demo", "terminal64.exe", ("XAUUSD", "#BTCUSD"), "t"
    )
    bridge = Bridge(
        emu,
        cfg,
        post,
        Journal(tmp_path / "journal.jsonl"),
        clock=lambda: datetime.fromtimestamp(emu.now_ms / 1000, tz=UTC),
    )
    return bridge, emu, [posted, queue]


def command(cid: str, kind: str, emu: MT5Emulator, **payload: Any) -> dict[str, Any]:
    return {"id": cid, "kind": kind, "payload": payload, "expires_at_ms": emu.now_ms + 20_000}


def outcome(entry: dict[str, Any]) -> dict[str, Any]:
    """A command's result without what bridge 8 adds to every one: its number and timings."""
    return {k: v for k, v in entry["result"].items() if k not in ("seq", "timing")}


def entry(**kw: Any) -> dict[str, Any]:
    base = {
        "client_order_id": ENTRY,
        "symbol": "XAUUSD",
        "side": "BUY",
        "purpose": "ENTRY",
        "order_type": "LIMIT",
        "volume": "0.01",
        "limit_price": "4101.00",
        "stop_price": None,
        "time_in_force": "GTC",
        "entry_client_order_id": ENTRY,
    }
    base.update(kw)
    return base


def test_magic_matches_the_server_and_fits_63_bits() -> None:
    assert magic(ENTRY) == server_magic(ENTRY) and 0 < magic(ENTRY) < 2**63


def test_snapshot_has_no_account_number_and_reports_demo(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    snap = bridge.snapshot()
    assert "12345678" not in json.dumps(snap)
    assert len(snap["account"]["fingerprint"]) == 16  # identifies the account, reveals nothing
    assert snap["account"]["trade_mode"] == "DEMO" and snap["account"]["margin_mode"] == "HEDGING"
    assert snap["ticks"]["XAUUSD"] == {"bid": "4100.0", "ask": "4100.3", "time_msc": emu.now_ms}
    assert snap["symbols"]["XAUUSD"]["contract_size"] == "100.0"
    gold = snap["symbols"]["XAUUSD"]  # version 3: what the swap numbers mean, and its day
    assert (gold["swap_long"], gold["swap_mode"], gold["swap_rollover3days"]) == ("-78.56", 1, 3)
    assert gold["point"] == "0.01" and snap["version"] == BRIDGE_VERSION


def test_marketable_entry_fills_once_and_is_found_again(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    r = bridge.run_command(command("c1", "SUBMIT", emu, **entry()))
    assert r["status"] == "DONE" and r["result"]["status"] == "FILLED"
    (pos,) = emu.positions.values()
    assert pos.magic == magic(ENTRY) and pos.volume == D("0.01")
    again = bridge.run_command(command("c2", "SUBMIT", emu, **entry()))  # same order, new command
    assert again["result"]["idempotent"] is True and len(emu.positions) == 1


def test_entry_below_the_market_rests_as_a_buy_limit(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    r = bridge.run_command(command("c1", "SUBMIT", emu, **entry(limit_price="4090.00")))
    assert r["result"]["status"] == "PLACED" and len(emu.orders) == 1
    emu.move("XAUUSD", "4089.70", "4090.00")
    assert not emu.orders and len(emu.positions) == 1
    cancel = bridge.run_command(
        command(
            "c2",
            "CANCEL",
            emu,
            client_order_id=ENTRY,
            purpose="ENTRY",
            symbol="XAUUSD",
            entry_client_order_id=ENTRY,
        )
    )
    assert cancel["result"]["status"] == "NOT_OPEN"


def test_ioc_entry_above_the_market_sends_nothing(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    r = bridge.run_command(
        command("c1", "SUBMIT", emu, **entry(limit_price="4090.00", time_in_force="IOC"))
    )
    assert r["result"]["status"] == "EXPIRED" and not emu.sent


def test_stop_is_the_positions_sl_and_never_a_short(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    bridge.run_command(command("c1", "SUBMIT", emu, **entry()))
    stop = dict(
        client_order_id=ENTRY.replace("-E", "-SL1"),
        symbol="XAUUSD",
        side="SELL",
        purpose="STOP_LOSS",
        order_type="STOP_LIMIT",
        volume="0.01",
        limit_price="3990.00",
        stop_price="4000.00",
        time_in_force="GTC",
        entry_client_order_id=ENTRY,
    )
    r = bridge.run_command(command("c2", "SUBMIT", emu, **stop))
    assert r["result"]["status"] == "PLACED"
    (pos,) = emu.positions.values()
    assert pos.sl == D("4000.00") and not emu.orders  # no separate sell order exists
    retry = bridge.run_command(command("c3", "SUBMIT", emu, **stop))  # same stop again
    assert retry["result"]["status"] == "PLACED"
    emu.move("XAUUSD", "3999.50", "3999.80")  # stop touched: the long is closed, nothing more
    assert not emu.positions and emu.deals[-1]["reason"] == emu.DEAL_REASON_SL


def test_exit_closes_only_what_the_entry_holds(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    bridge.run_command(command("c1", "SUBMIT", emu, **entry(volume="0.02")))
    exit_ = dict(
        client_order_id=ENTRY.replace("-E", "-X1"),
        symbol="XAUUSD",
        side="SELL",
        purpose="EXIT",
        order_type="LIMIT",
        volume="0.02",
        limit_price="4099.00",
        stop_price=None,
        time_in_force="IOC",
        entry_client_order_id=ENTRY,
    )
    low = bridge.run_command(command("c2", "SUBMIT", emu, **{**exit_, "limit_price": "4100.50"}))
    assert low["result"]["status"] == "EXPIRED" and len(emu.positions) == 1  # bid below limit
    r = bridge.run_command(command("c3", "SUBMIT", emu, **exit_))
    assert r["result"]["status"] == "FILLED" and not emu.positions
    sell = emu.sent[-1]
    assert sell["type"] == emu.ORDER_TYPE_SELL and "position" in sell
    none_left = bridge.run_command(
        command("c4", "SUBMIT", emu, **{**exit_, "client_order_id": ENTRY.replace("-E", "-X2")})
    )
    assert none_left["result"]["status"] == "REJECTED"  # nothing held: no sell is ever sent


def test_demo_only_and_deadlines(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path, MT5Emulator(trade_mode=ACCOUNT_TRADE_MODE_REAL))
    r = bridge.run_command(command("c1", "SUBMIT", emu, **entry()))
    assert r["status"] == "FAILED" and "demo" in r["error"] and not emu.sent
    assert outcome(bridge.run_command(command("c2", "PING", emu))) == {"demo": False}
    bridge, emu, _ = make(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    late = {**command("c3", "SUBMIT", emu, **entry()), "expires_at_ms": emu.now_ms - 1}
    assert bridge.run_command(late)["status"] == "FAILED" and not emu.sent


@pytest.mark.parametrize(
    ("fault", "status", "positions"), [("timeout_after", "FILLED", 1), ("none", "UNKNOWN", 0)]
)
def test_uncertain_answers_are_resolved_by_magic(
    tmp_path: Path, fault: str, status: str, positions: int
) -> None:
    bridge, emu, _ = make(tmp_path)
    emu.faults.append(fault)
    r = bridge.run_command(command("c1", "SUBMIT", emu, **entry()))
    assert r["result"]["status"] == status and len(emu.positions) == positions
    retry = bridge.run_command(command("c2", "SUBMIT", emu, **entry()))
    assert len(emu.positions) == 1  # never two: a retry finds it, or sends it once
    assert retry["result"]["status"] == "FILLED"


def test_a_command_delivered_twice_runs_once_and_results_survive_failures(tmp_path: Path) -> None:
    bridge, emu, (posted, queue) = make(tmp_path)
    cmd = command("c1", "SUBMIT", emu, **entry())
    queue.append([cmd])
    assert bridge.cycle() == 1
    queue.append([cmd])  # delivered again
    bridge.cycle()
    assert len([s for s in emu.sent if s["action"] == emu.TRADE_ACTION_DEAL]) == 1
    assert posted[1]["results"][0]["command_id"] == "c1"  # reported in the next sync
    reopened = Journal(tmp_path / "journal.jsonl")
    assert "c1" in reopened.done  # survives a restart


def test_the_broker_clock_offset_is_learned_from_fresh_ticks_only(tmp_path: Path) -> None:
    """MT5 stamps ticks in server time (HFM: UTC+3). A fresh tick fixes the offset to whole
    15-minute steps; a stale one never changes it."""
    emu = MT5Emulator(server_offset_ms=3 * 3_600_000 + 1_234)  # +3 h, the tick 1.2 s old
    bridge, emu, _ = make(tmp_path, emu)
    assert bridge.snapshot()["server_utc_offset_ms"] == 10_800_000
    emu.server_offset_ms = 3 * 3_600_000 - 50 * 60_000  # a 50-minute-old tick
    assert bridge.snapshot()["server_utc_offset_ms"] == 10_800_000  # kept, not re-learned
    fresh, _, _ = make(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, emu)
    assert fresh.snapshot()["server_utc_offset_ms"] is None  # never trusted a stale tick


def test_a_closed_market_never_resets_the_offset_as_its_last_tick_ages(tmp_path: Path) -> None:
    """Bridge 13 (2026-11-22: terminal 2 read -14 hours over the weekend). The last tick of a
    closed market passes a whole quarter of an hour of age every 15 minutes; it is not a new
    tick, so the offset learned from live prices stays, and it is learned again at the reopen."""
    two_hours = 2 * 3_600_000
    emu = MT5Emulator(server_offset_ms=two_hours)
    bridge, emu, _ = make(tmp_path, emu)
    assert bridge.snapshot()["server_utc_offset_ms"] == two_hours
    for aged in (15 * 60_000, 60 * 60_000, 14 * 3_600_000):  # Friday's close, ageing
        emu.now_ms += aged
        emu.server_offset_ms -= aged  # the same tick: its server time does not move
        assert bridge.snapshot()["server_utc_offset_ms"] == two_hours
        emu.now_ms -= aged
        emu.server_offset_ms += aged
    emu.now_ms += 3 * 3_600_000
    emu.server_offset_ms = two_hours + 1_000  # the market reopens: a new tick, 1 s old
    assert bridge.snapshot()["server_utc_offset_ms"] == two_hours


def test_the_report_says_whether_the_terminal_is_read_only(tmp_path: Path) -> None:
    """Bridge 13: the server shows the REAL account as connected only when its terminal says
    it refuses every trading command."""
    bridge, _, _ = make(tmp_path)
    assert bridge.snapshot()["terminal"]["read_only"] is False
    ro = Bridge(
        MT5Emulator(),
        Config("https://example.invalid", "hfm-real", "t", ("XAUUSD",), "t", read_only=True),
        lambda p: {},
        Journal(tmp_path / "ro.jsonl"),
    )
    assert ro.snapshot()["terminal"]["read_only"] is True


@pytest.mark.parametrize("retcode", [10009, 10008])
def test_a_pending_order_is_placed_whatever_the_success_code(tmp_path: Path, retcode: int) -> None:
    """HFM's real server answered a placed BUY_LIMIT with 10009 DONE and deal 0 (2026-10-15),
    not 10008 PLACED: only a deal is a fill."""
    bridge, emu, _ = make(tmp_path, MT5Emulator(pending_retcode=retcode))
    r = bridge.run_command(command("c1", "SUBMIT", emu, **entry(limit_price="3300.00")))
    assert r["result"]["status"] == "PLACED" and len(emu.orders) == 1 and not emu.positions


STOP = dict(
    client_order_id=ENTRY.replace("-E", "-SL1"),
    symbol="XAUUSD",
    side="SELL",
    purpose="STOP_LOSS",
    order_type="STOP_LIMIT",
    volume="0.01",
    limit_price="3990.00",
    stop_price="4000.00",
    time_in_force="GTC",
    entry_client_order_id=ENTRY,
)


def test_the_stop_loss_travels_with_the_entry(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    r = bridge.run_command(command("c1", "SUBMIT", emu, **entry(attached_sl="4000.00")))
    assert r["result"]["status"] == "FILLED" and emu.sent[-1]["sl"] == 4000.0
    (pos,) = emu.positions.values()
    assert pos.sl == D("4000.00")  # held by the broker from the fill, whatever happens next
    # The protection step's stop then finds it already there.
    again = bridge.run_command(command("c2", "SUBMIT", emu, **STOP))
    assert again["result"]["status"] == "PLACED" and pos.sl == D("4000.00")


def test_a_resting_entry_carries_its_stop_into_the_position(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    low = entry(limit_price="4000.00", attached_sl="3900.00")
    assert bridge.run_command(command("c1", "SUBMIT", emu, **low))["result"]["status"] == "PLACED"
    (order,) = emu.orders.values()
    assert order["sl"] == 3900.0
    assert bridge.snapshot()["orders"][0]["sl"] == "3900.0"  # what the contract check reads
    emu.move("XAUUSD", "3999.70", "4000.00")  # the limit fills
    (pos,) = emu.positions.values()
    assert pos.sl == D("3900.00")


def test_a_stop_at_or_above_the_entry_is_refused_before_sending(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    for sl in ("4101.00", "5000", "0"):
        r = bridge.run_command(command(f"c{sl}", "SUBMIT", emu, **entry(attached_sl=sl)))
        assert outcome(r) == {
            "status": "REJECTED",
            "comment": "the stop-loss must be below the entry",
        }
    assert not emu.sent


def test_a_stop_order_after_the_stop_closed_the_position_reports_the_fill(tmp_path: Path) -> None:
    bridge, emu, _ = make(tmp_path)
    bridge.run_command(command("c1", "SUBMIT", emu, **entry(attached_sl="4000.00")))
    emu.move("XAUUSD", "3999.00", "3999.30")  # HFM's stop closes it before the stop order
    assert not emu.positions
    r = bridge.run_command(command("c2", "SUBMIT", emu, **STOP))
    assert r["result"]["status"] == "FILLED"  # booked as the stop's fill, not a failed stop
    other = dict(STOP, entry_client_order_id="ct-T-9-E", client_order_id="ct-T-9-SL1")
    refused = bridge.run_command(command("c3", "SUBMIT", emu, **other))
    assert refused["result"]["status"] == "REJECTED"  # no position and no stop-out: refused


def test_hfms_candles_are_sent_once_and_never_kept_in_the_journal(tmp_path: Path) -> None:
    """Version 4's RATES is read-only and large (thousands of candles an hour): it is sent with the
    next sync but never journaled, so the journal does not grow with it (review, S61)."""
    bridge, emu, (posted, queue) = make(tmp_path)
    emu.candles[("XAUUSD", emu.TIMEFRAME_H1)] = [
        {
            "time": 1_790_000_000 + 3600 * k,
            "open": 4000.0,
            "high": 4010.0,
            "low": 3990.0,
            "close": 4005.0,
            "tick_volume": 100,
        }
        for k in range(3)
    ]
    queue.append([command("r1", "RATES", emu, symbol="XAUUSD", timeframe="H1", count=2)])
    assert bridge.cycle() == 1
    bridge.cycle()
    (result,) = posted[1]["results"]
    assert result["command_id"] == "r1" and len(result["result"]["rates"]) == 2
    assert result["result"]["rates"][-1][1:5] == ["4000.0", "4010.0", "3990.0", "4005.0"]
    assert "r1" not in bridge.journal.done
    assert (
        not (tmp_path / "journal.jsonl").exists()
        or "r1" not in (tmp_path / "journal.jsonl").read_text()
    )
    assert not emu.sent  # nothing traded


# ---- version 8 (S87): the fast lane, an order's time limit, the order of things ---------------


def test_an_order_past_its_time_is_not_sent_and_the_limit_is_this_computers_own(
    tmp_path: Path,
) -> None:
    """The owner's rule (2026-11-12): an order leaves within a second of its prediction, or not
    at all. The server says how long is left (``ttl_ms``); it is counted here from the order's
    arrival, on this computer's clock, so two computers' clocks never decide it."""
    bridge, emu, _ = make(tmp_path)
    fast = entry(time_in_force="IOC", attached_sl="4098.40", attached_tp="4104.30")
    late = command("c1", "SUBMIT", emu, **fast, ttl_ms=200)
    r = bridge.run_command(late, received=time.monotonic() - 0.5, lane="fast")  # 0.5 s ago
    assert r["status"] == "FAILED" and r["error"].startswith("late:") and not emu.sent
    bridge.journal.record(r)  # as both lanes do with every answer
    again = bridge.run_command(late)  # delivered twice: the same answer, never run after all
    assert again["error"].startswith("late:") and not emu.sent
    # In time: sent, whatever the server's own deadline says on a clock that is not this one.
    ok = {**command("c2", "SUBMIT", emu, **fast, ttl_ms=5_000), "expires_at_ms": emu.now_ms - 1}
    r = bridge.run_command(ok, received=time.monotonic(), lane="fast")
    (pos,) = emu.positions.values()
    assert r["result"]["status"] == "FILLED" and (pos.sl, pos.tp) == (D("4098.40"), D("4104.30"))
    timing = r["result"]["timing"]
    assert timing["lane"] == "fast" and 0 <= timing["wait_ms"] <= timing["total_ms"] < 1000
    assert timing["send_ms"] is not None
    # Each finished command has its number, in the order they finished, in this run.
    assert r["result"]["seq"] == [bridge.epoch_ms, 2]
    assert bridge.snapshot()["commands_done"] == [bridge.epoch_ms, 2]


def test_the_fast_lane_sends_prices_runs_orders_at_once_and_reports_them_once(
    tmp_path: Path,
) -> None:
    bridge, emu, (posted, _queue) = make(tmp_path)
    assert bridge.cycle() == 0 and "commands_done" not in posted[-1]  # an older server: as before
    assert bridge.fast_offer is None
    order = command("c1", "SUBMIT", emu, **entry(time_in_force="IOC"), ttl_ms=5_000)
    answers = [
        {"commands": [order], "received_ms": emu.now_ms + 30, "replied_ms": emu.now_ms + 35},
        {"commands": []},
    ]
    asked: list[dict[str, Any]] = []

    def lane(payload: dict[str, Any]) -> dict[str, Any]:
        asked.append(json.loads(json.dumps(payload)))
        return answers.pop(0)

    bridge.fast_offer = {"hold_ms": 250}  # the server's answer to a report offers the lane
    assert bridge.fast_step(lane) == 1 and len(emu.positions) == 1  # run at once
    first = asked[0]
    assert set(first) == {
        "bridge", "version", "sent_at_ms", "server_utc_offset_ms", "ticks", "results",
        "hold_ms", "info",
    }  # fmt: skip
    assert first["ticks"]["XAUUSD"]["bid"] == "4100.0" and first["hold_ms"] == 250
    assert "account" not in first and bridge.wake.is_set()  # prices only; the report follows
    assert "rtt_ms" in bridge.fast_info and "clock_offset_ms" in bridge.fast_info
    # Its result goes back on the lane's next request, which does not wait; the report, if it
    # came first, would have carried it too; either way it is sent until one has delivered it.
    assert [r["command_id"] for r in bridge.journal.waiting()] == ["c1"]
    assert bridge.fast_step(lane) == 0
    assert asked[1]["hold_ms"] == 0 and [r["command_id"] for r in asked[1]["results"]] == ["c1"]
    assert bridge.journal.waiting() == [] and bridge.journal.waiting(fast=True) == []
    # A report to a server with the lane says how many commands were finished when it was read.
    bridge.cycle()
    assert posted[-1]["commands_done"] == [bridge.epoch_ms, 1] and posted[-1]["results"] == []


class Watched:
    """The emulator, noting which thread makes each MT5 call."""

    def __init__(self, emu: MT5Emulator) -> None:
        self._emu = emu
        self.threads: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._emu, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            self.threads.add(threading.current_thread().name)
            return attr(*args, **kwargs)

        return call


def test_three_threads_run_the_bridge_and_only_one_ever_calls_mt5(tmp_path: Path) -> None:
    """The bridge as it runs on the bridge computer (``serve``): the report and the lanes
    each wait on the network in their own thread and pass what arrives to this one, the only
    one that calls MT5. An order on the fast lane is sent while a report's request is still in
    flight; its result goes back on the lane; the report that follows has reached its number."""
    emu = MT5Emulator()
    watched = Watched(emu)
    cfg = Config(
        "https://example.invalid",
        "hfm-demo",
        "terminal64.exe",
        ("XAUUSD",),
        "t",
        interval_seconds=0.05,
    )
    reports: list[dict[str, Any]] = []
    asked: list[dict[str, Any]] = []
    order = command("c1", "SUBMIT", emu, **entry(time_in_force="IOC"), ttl_ms=5_000)
    orders = [order]
    stop = threading.Event()
    seen: dict[str, Any] = {}

    def report(payload: dict[str, Any]) -> dict[str, Any]:
        reports.append(json.loads(json.dumps(payload)))
        time.sleep(0.3)  # a slow request: an order must not wait for it
        return {"commands": [], "fast": {"hold_ms": 20}}

    hand_out = threading.Lock()
    lanes: list[int] = []

    def lane(payload: dict[str, Any]) -> dict[str, Any]:
        asked.append({**json.loads(json.dumps(payload)), "by": threading.current_thread().name})
        if payload["results"]:
            seen.setdefault("result", payload["results"][0])
        time.sleep(0.02)  # the server's hold
        with hand_out:  # the server hands an order out once, to whichever lane asks first
            if len(reports) >= 2 and orders:  # while the second report's request is in flight
                seen["handed"] = time.monotonic()
                return {"commands": [orders.pop()]}
        return {"commands": []}

    def new_lane() -> Any:
        lanes.append(1)  # each lane asks for its own way to the server
        return lane

    real_send = emu.order_send

    def order_send(request: dict[str, Any]) -> Any:
        seen.setdefault("sent", time.monotonic())
        return real_send(request)

    emu.order_send = order_send  # type: ignore[method-assign]
    bridge = Bridge(
        watched,
        cfg,
        report,
        Journal(tmp_path / "journal.jsonl"),
        clock=lambda: datetime.fromtimestamp(emu.now_ms / 1000, tz=UTC),
    )

    def enough() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            done = (
                "result" in seen
                and any(r.get("commands_done", [0, 0])[1] >= 1 for r in reports)
                and len({a["by"] for a in asked if not a["results"]}) == 4  # every lane asked
            )
            if done:
                break
            time.sleep(0.02)
        stop.set()

    threading.Thread(target=enough, name="test-watch").start()
    serve(bridge, new_lane, stop)  # this thread is the MT5 thread
    assert watched.threads == {threading.current_thread().name}  # no other thread called MT5
    # Version 9: three order lanes the server may hold for a second each, and a price lane it
    # never holds, each on its own connection.
    assert len(lanes) == 4
    holds = {a["by"]: a["hold_ms"] for a in asked if not a["results"]}
    assert {v for k, v in holds.items() if k.startswith("order-lane")} == {1000}
    assert holds.get("price-lane") == 0 and len(emu.sent) == 1  # the order was sent once
    assert len(emu.positions) == 1 and seen["result"]["command_id"] == "c1"
    assert seen["sent"] - seen["handed"] < 0.25  # sent at once, not after the report's request
    timing = seen["result"]["result"]["timing"]
    assert timing["lane"] == "fast" and timing["wait_ms"] < 250
    assert asked[0]["ticks"]["XAUUSD"]["bid"] == "4100.0"  # the prices the MT5 thread read
    shown = [r for r in reports if r.get("commands_done", [0, 0])[1] >= 1]
    assert shown and shown[0]["positions"]  # the report that reached it shows the position


def test_a_report_shows_a_command_only_once_it_has_reached_its_number() -> None:
    """With two lanes a result no longer arrives with the report that shows what it did."""
    at = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    done = SimpleNamespace(result={"seq": [1000, 5]}, finished_at=at)
    assert not reflects({"commands_done": [1000, 4]}, done, at)  # read before it finished
    assert reflects({"commands_done": [1000, 5]}, done, at)
    assert reflects({"commands_done": [2000, 0]}, done, at)  # the bridge restarted since
    # A number never reached (the bridge computer's clock set back) is given up after a minute.
    assert not reflects({"commands_done": [900, 0]}, done, at + timedelta(seconds=59))
    assert reflects({"commands_done": [900, 0]}, done, at + timedelta(seconds=61))
    # Before bridge 8 they travelled together: the report arrived no earlier than the result.
    old = SimpleNamespace(result={"status": "FILLED"}, finished_at=at)
    assert reflects({}, old, at) and not reflects({}, old, at - timedelta(seconds=1))


# ---- version 10 (S91): the direct link, with the internet path as its fallback ----------------


def _direct(tmp_path: Path, **kw: Any) -> Direct:
    raw = {
        "host": "192.0.2.10",
        "user": "cosmicbridge",
        "key_file": "link_key",
        "known_hosts_file": "link_known_hosts",
        **kw,
    }
    return Direct.load(tmp_path, raw)


def _up(link: Link, port: int = 4711) -> Link:
    """The link as its own thread leaves it once the tunnel listens."""
    link._port = port
    return link


def test_a_request_goes_on_the_direct_link_and_over_the_internet_the_moment_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's rule (2026-11-12) with two ways to the server: the direct link when it is
    up, and no gap when it is not. Each request carries the measurements of the way it takes,
    and an order's record names the way it came by."""
    bridge, emu, _ = make(tmp_path)
    bridge.fast_offer = {"hold_ms": 250}
    link = _up(Link(_direct(tmp_path)))
    broken: list[bool] = []
    seen: list[tuple[str, dict[str, Any]]] = []
    orders: list[dict[str, Any]] = []

    def answer(way_ms: int) -> dict[str, Any]:
        """As the server answers; the bridge reads the round trip as ``way_ms`` and what the
        request took here (the two times say how long the server held it: minus that)."""
        return {
            "commands": [orders.pop()] if orders else [],
            "received_ms": emu.now_ms,
            "replied_ms": emu.now_ms - way_ms,
        }

    def direct(payload: dict[str, Any]) -> dict[str, Any]:
        if broken:
            raise ConnectionRefusedError("the tunnel is gone")
        seen.append(("direct", json.loads(json.dumps(payload))))
        return answer(3)

    def internet(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(("internet", json.loads(json.dumps(payload))))
        return answer(380)

    lane = two_ways(link, direct, internet, bridge.info_for)
    assert bridge.fast_step(lane) == 0 and seen[-1][0] == "direct"
    assert seen[-1][1]["info"] == {"path": "direct"}  # nothing measured on this way yet
    assert bridge.fast_info["path"] == "direct" and "rtt_ms" in bridge.path_info["direct"]
    # The link fails under a request: the same request goes over the internet at once, and
    # for the next seconds the lanes do not try the link at all.
    broken.append(True)
    assert bridge.fast_step(lane) == 0 and seen[-1][0] == "internet"
    assert link.port() is None and "internet" not in bridge.path_info  # sent twice: no measure
    assert bridge.fast_step(lane) == 0 and seen[-1][0] == "internet"
    assert bridge.path_info["internet"]["path"] == "internet"
    assert 3 <= bridge.path_info["direct"]["rtt_ms"] < 100  # each way's own round trip
    assert 380 <= bridge.path_info["internet"]["rtt_ms"] < 480
    # An order arriving now came over the internet: its request carried that way's round trip
    # (the server allows for it in the order's remaining time), and its record says so too.
    orders.append(command("c1", "SUBMIT", emu, **entry(time_in_force="IOC"), ttl_ms=5_000))
    assert bridge.fast_step(lane) == 1 and 380 <= seen[-1][1]["info"]["rtt_ms"] < 480
    (done,) = bridge.journal.waiting(fast=True)
    timing = done["result"]["timing"]
    assert timing["lane"] == "fast" and 380 <= timing["rtt_ms"] < 480
    # The tunnel is back and the wait is over: the link again, with its own measurements.
    broken.clear()
    monkeypatch.setattr(link, "_down_until", 0.0)
    emu.positions.clear()
    orders.append(command("c2", "SUBMIT", emu, **entry(time_in_force="IOC"), ttl_ms=5_000))
    assert bridge.fast_step(lane) == 1 and seen[-1][0] == "direct"
    assert seen[-1][1]["info"]["path"] == "direct" and 3 <= seen[-1][1]["info"]["rtt_ms"] < 100
    timing = bridge.journal.done["c2"]["result"]["timing"]
    assert timing["lane"] == "direct" and 3 <= timing["rtt_ms"] < 100
    assert [r["command_id"] for r in bridge.journal.waiting(fast=True)] == ["c2"]  # c1 delivered


def test_the_report_takes_the_same_two_ways(tmp_path: Path) -> None:
    emu = MT5Emulator()
    link = _up(Link(_direct(tmp_path)))
    sent: list[str] = []

    def way(name: str, fail: bool = False) -> Any:
        def post(payload: dict[str, Any]) -> dict[str, Any]:
            if fail:
                raise TimeoutError("no answer on the link")
            sent.append(name)
            assert "info" not in payload  # a report is sent as it is on either way
            return {"commands": []}

        return post

    cfg = Config("https://example.invalid", "hfm-demo", "terminal64.exe", ("XAUUSD",), "t")
    journal = Journal(tmp_path / "journal.jsonl")
    Bridge(emu, cfg, two_ways(link, way("direct"), way("internet")), journal).cycle()
    Bridge(emu, cfg, two_ways(link, way("direct", fail=True), way("internet")), journal).cycle()
    assert sent == ["direct", "internet"]


def test_the_tunnel_is_one_ssh_command_to_the_pinned_server_and_nothing_else(
    tmp_path: Path,
) -> None:
    d = _direct(tmp_path)
    args = Link(d).command(50123)
    assert args[0] == "ssh" and args[-1] == "cosmicbridge@192.0.2.10"
    assert args[1:5] == ["-F", "none", "-N", "-T"]  # no other configuration, command or terminal
    options = [args[i + 1] for i, a in enumerate(args) if a == "-o"]
    assert "BatchMode=yes" in options and "StrictHostKeyChecking=yes" in options
    assert f"UserKnownHostsFile={tmp_path / 'link_known_hosts'}" in options  # the pinned key
    assert "ExitOnForwardFailure=yes" in options and "IdentitiesOnly=yes" in options
    assert "ServerAliveInterval=5" in options  # a dead tunnel ends by itself
    assert not any(o.startswith("ConnectTimeout") for o in options)  # see ``Link.command``
    assert args[args.index("-i") + 1] == str(tmp_path / "link_key")
    assert args[args.index("-L") + 1] == "127.0.0.1:50123:127.0.0.1:8745"  # loopback to loopback
    # Every setting becomes an argument of that command: none may be read as an option.
    for bad in (
        {"host": "-oProxyCommand=calc"},
        {"host": "a b"},
        {"user": "root -p"},
        {"user": "-l"},
        {"port": 22},
    ):
        with pytest.raises(ValueError, match="direct"):
            _direct(tmp_path, **bad)
    # config.json without the link: the bridge runs as before version 10.
    (tmp_path / "token.txt").write_text("t\n")
    base = {"url": "https://x.invalid", "terminal": "t.exe", "symbols": ["XAUUSD"]}
    (tmp_path / "config.json").write_text(json.dumps({**base, "token_file": "token.txt"}))
    assert Config.load(tmp_path / "config.json").direct is None
    linked = {
        "host": "192.0.2.10",
        "user": "cosmicbridge",
        "key_file": "k",
        "known_hosts_file": "h",
    }
    (tmp_path / "config.json").write_text(
        json.dumps({**base, "token_file": "token.txt", "direct": linked})
    )
    assert Config.load(tmp_path / "config.json").direct == Direct(
        "192.0.2.10", "cosmicbridge", tmp_path / "k", tmp_path / "h"
    )


class Tunnel:
    """A stand-in for the ssh process: running until told to end."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stderr = [b"Connection to 192.0.2.10 closed.\n"]  # read line by line
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


def test_the_tunnel_is_started_again_when_it_ends_and_ended_when_the_bridge_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(program, "LINK_RESTART", 0.02)
    started: list[tuple[list[str], Tunnel]] = []
    ports = iter(range(50001, 50100))

    def popen(args: list[str], **kw: Any) -> Tunnel:
        assert kw["stdin"] == program.subprocess.DEVNULL  # nothing can be typed into it
        started.append((args, Tunnel()))
        return started[-1][1]

    link = Link(
        _direct(tmp_path), popen=popen, free_port=lambda: next(ports), listening=lambda p: True
    )
    assert link.port() is None  # before the tunnel is up: the internet path
    stop = threading.Event()
    thread = threading.Thread(target=link.run, args=(stop,))
    thread.start()
    try:
        _until(lambda: link.port() == 50001)
        started[0][1].returncode = 255  # the server restarted, or the network dropped
        _until(lambda: link.port() == 50002)  # a new tunnel, on a port of its own
        assert len(started) == 2 and "127.0.0.1:50002:127.0.0.1:8745" in started[1][0]
        link.failed(OSError("no answer"))  # a request failed on a tunnel that still runs
        assert link.port() is None  # the internet path for the next seconds
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and started[1][1].terminated and link.port() is None

    # A tunnel that never comes up is tried ever more rarely, not every few seconds for ever.
    waits: list[float] = []

    class Stop:
        def is_set(self) -> bool:
            return len(waits) >= 5

        def wait(self, seconds: float) -> None:
            if seconds >= 0.02:  # the waits between tries, not the looks at a starting tunnel
                waits.append(seconds)

    def refused(args: list[str], **kw: Any) -> Tunnel:
        tunnel = Tunnel()
        tunnel.returncode = 255  # "Permission denied": it ends at once
        return tunnel

    Link(_direct(tmp_path), popen=refused, free_port=lambda: 50200, listening=lambda p: False).run(
        Stop()  # type: ignore[arg-type]
    )
    assert waits == [0.04, 0.08, 0.16, 0.32, 0.64]


def _until(done: Any, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, "not in time"
        time.sleep(0.005)


def test_a_request_on_the_link_leaves_in_one_piece() -> None:
    """Head and body in one write: through a tunnel a second small piece can wait tens of
    milliseconds for the first to be acknowledged."""
    written: list[bytes] = []
    conn = program._Whole("127.0.0.1", 1)
    conn.sock = SimpleNamespace(sendall=written.append)  # type: ignore[assignment]
    conn.request("POST", "/bridge/v1/fast", b'{"n": 1}', {"Content-Type": "application/json"})
    (piece,) = written
    assert piece.startswith(b"POST /bridge/v1/fast HTTP/1.1\r\n")
    assert piece.endswith(b'\r\n\r\n{"n": 1}') and b"Content-Length: 8\r\n" in piece
    # A request without a body is not held back for ever: it goes when the answer is asked for.
    conn = program._Whole("127.0.0.1", 1)
    conn.sock = SimpleNamespace(sendall=written.append)  # type: ignore[assignment]
    conn.request("GET", "/x")
    assert len(written) == 1
    with pytest.raises(Exception):  # noqa: B017 (no server here: only the write matters)
        conn.getresponse()
    assert len(written) == 2 and written[1].startswith(b"GET /x HTTP/1.1\r\n")


def test_a_request_on_the_link_is_plain_http_to_the_loopback_with_the_bridges_token_only(
    tmp_path: Path,
) -> None:
    """Against a real HTTP server on this machine's loopback, standing in for the tunnel's
    end: one connection kept open, the bridge's token, none of the internet path's other
    credentials, and a new connection when the kept one was closed."""
    got: list[tuple[str, dict[str, str], dict[str, Any], int]] = []

    class Relay(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append((self.path, dict(self.headers.items()), body, self.client_address[1]))
            self.close_connection = bool(body.get("then_close"))  # without saying so
            data = json.dumps({"commands": []}).encode()
            self.send_response(200 if self.path.startswith("/bridge/v1/") else 404)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cfg = Config(
            "https://portal.example.invalid",
            "hfm-demo",
            "terminal64.exe",
            ("XAUUSD",),
            "the-token",
            cf_access={"client_id": "access-id", "client_secret": "access-secret"},
        )
        link = _up(Link(_direct(tmp_path)), server.server_address[1])
        post = direct_post(cfg, link, "/bridge/v1/fast", 3.0)
        assert post({"n": 1}) == {"commands": []}
        assert post({"n": 2, "then_close": True}) == {"commands": []}
        (path, headers, body, port), (_, _, _, again) = got
        assert path == "/bridge/v1/fast" and body == {"n": 1} and port == again  # kept open
        assert headers["Authorization"] == "Bearer the-token" and headers["Host"] == "127.0.0.1"
        assert not any("access" in k.lower() or "access" in v for k, v in headers.items())
        # The far end has closed the kept connection (the portal restarted): the request is
        # sent once more, on a new one.
        assert post({"n": 3}) == {"commands": []}
        assert [g[2]["n"] for g in got] == [1, 2, 3] and got[2][3] != port
        # With the tunnel down a lane opens nothing: its request goes over the internet.
        link._port = None
        with pytest.raises(OSError, match="the direct link is down"):
            direct_post(cfg, link, "/bridge/v1/fast", 3.0)({"n": 4})
        assert len(got) == 3
    finally:
        server.shutdown()
        server.server_close()


# ---- version 11 (S94): the fallback takes over at once -----------------------------------------
# The night of 2026-11-17: the trading computer's network paused for six seconds (a Windows
# security update). The link was back by itself; but each lane's request over the internet path
# went out on a connection that had lain unused for 19 hours, and waited 15 seconds for an
# answer that could not come. The lanes carried nothing for 18 seconds.


def test_an_answer_is_waited_for_as_long_as_its_request_may_be_held_and_a_little() -> None:
    """Not a fixed 15 seconds: the request's own hold, and the path's patience."""
    made: list[Any] = []

    class Conn:
        def __init__(self) -> None:
            self.timeout: float | None = None
            self.sock: Any = None
            self.waits: list[float] = []
            self.closed = False
            made.append(self)

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            self.sock = self.sock or SimpleNamespace(settimeout=self.waits.append)

        def getresponse(self) -> Any:
            return SimpleNamespace(status=200, read=lambda: b'{"commands": []}')

        def close(self) -> None:
            self.closed = True

    post = program._kept(Conn, "/bridge/v1/fast", {}, "the fast lane", 4.0, fresh=0.05)  # type: ignore[arg-type]
    post({"hold_ms": 1000})
    assert made[0].timeout == 5.0  # an order lane: its second at the server, and four
    post({"hold_ms": 0, "results": [1]})
    assert len(made) == 1 and made[0].waits == [4.0]  # the same connection, this request's wait
    post({})  # a report has no hold
    assert made[0].waits == [4.0, 4.0]
    # Unused for longer than it may lie: closed, and a new one is opened for the request.
    time.sleep(0.06)
    post({"hold_ms": 1000})
    assert len(made) == 2 and made[0].closed and made[1].timeout == 5.0
    assert program.DIRECT_PATIENCE == 1.0 and program.INTERNET_PATIENCE == 4.0


class _Swallows(socketserver.StreamRequestHandler):
    """Answers a connection's first request; whatever else comes on that connection is taken
    and never answered, as on a connection dropped somewhere on the way. A new connection is
    answered again."""

    def handle(self) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.connection.recv(65536)
        body = b'{"commands": []}'
        self.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        while self.connection.recv(65536):
            pass


def test_a_connection_that_lay_unused_is_not_trusted_and_a_dead_one_costs_little() -> None:
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 0), _Swallows)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:

        def lane(fresh: float) -> Any:
            return program._kept(
                lambda: http.client.HTTPConnection("127.0.0.1", port),
                "/bridge/v1/fast",
                {"Content-Type": "application/json"},
                "the fast lane",
                0.4,
                fresh,
            )

        # As bridge 11 does: the connection lay unused, so the request opens a new one, and is
        # answered at once.
        post = lane(fresh=0.1)
        assert post({"n": 1}) == {"commands": []}
        time.sleep(0.15)
        started = time.monotonic()
        assert post({"n": 2}) == {"commands": []}
        assert time.monotonic() - started < 0.3
        # And a kept connection that is dead although it was just used (nothing can know):
        # the request gives up after its hold and the path's patience, not after 15 seconds.
        post = lane(fresh=999.0)
        assert post({"n": 1}) == {"commands": []}
        started = time.monotonic()
        with pytest.raises(OSError):
            post({"n": 2, "hold_ms": 200})
        assert 0.55 <= time.monotonic() - started < 1.2
        assert post({"n": 3}) == {"commands": []}  # the next one, on a new connection
    finally:
        server.shutdown()
        server.server_close()


def test_a_link_that_stops_answering_is_left_within_its_patience(tmp_path: Path) -> None:
    """The link's tunnel is up but nothing comes back through it: the request goes over the
    internet path after the hold and one path's patience, and the lanes stay off the link for
    the next seconds."""
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)  # takes connections, answers nothing
    try:
        link = _up(Link(_direct(tmp_path)), silent.getsockname()[1])
        cfg = Config("https://portal.example.invalid", "hfm-demo", "t.exe", ("XAUUSD",), "t")
        post = two_ways(
            link,
            direct_post(cfg, link, "/bridge/v1/fast", 0.3),
            lambda payload: {"commands": [], "hold_ms": payload["hold_ms"]},
        )
        started = time.monotonic()
        answer = post({"hold_ms": 100})
        took = time.monotonic() - started
        assert answer == {"commands": [], "hold_ms": 100, "via": "internet", "retried": True}
        assert 0.35 <= took < 1.0, took
        assert link.port() is None
        started = time.monotonic()
        assert post({"hold_ms": 100})["via"] == "internet"  # at once: the link is not tried
        assert time.monotonic() - started < 0.1
    finally:
        silent.close()


# ---- version 12 (S95): a held window never stops the bridge -------------------------------------
# 2026-11-17: the bridge's window was held from 05:19 UTC (Windows holds a console program that
# writes while text is selected in its window). Every thread that then wrote a line stopped: the
# lanes at once, the report at 05:59, and the bridge was down until it was restarted.


class HeldWindow:
    """A console that takes no line while ``held`` is set, as Windows does with a selection."""

    def __init__(self) -> None:
        self.held = threading.Event()
        self.held.set()
        self.free = threading.Event()
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        while self.held.is_set():  # blocked, as WriteFile on a held console is
            self.free.wait(0.01)
        self.lines.append(text)

    def flush(self) -> None:
        pass


@pytest.fixture
def own_logging() -> Any:
    import logging

    root = logging.getLogger()
    before = (list(root.handlers), root.level, logging.raiseExceptions)
    writers: list[Any] = []
    yield writers
    for w in writers:
        w.enqueue_sentinel()  # the writer ends once the window takes lines again
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in before[0]:
        root.addHandler(h)
    root.setLevel(before[1])
    logging.raiseExceptions = before[2]


def test_a_held_window_never_stops_the_bridge(tmp_path: Path, own_logging: list[Any]) -> None:
    window = HeldWindow()
    writer = program.logging_to(tmp_path, window)
    own_logging.append(writer)
    log = program.log
    done: list[str] = []

    def a_lane(n: int) -> None:
        for i in range(400):  # more lines than the window's queue holds
            log.warning("a fast lane failed (%d.%d); the others and the report go on", n, i)
        done.append(f"lane {n}")

    threads = [threading.Thread(target=a_lane, args=(n,)) for n in range(4)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(done) == ["lane 0", "lane 1", "lane 2", "lane 3"]  # none of them stopped
    assert time.monotonic() - started < 5
    # Every line is in bridge.log, and it says once why the window shows none of them.
    text = (tmp_path / "bridge.log").read_text(encoding="utf-8")
    assert text.count("a fast lane failed") == 1600
    assert text.count("the bridge's window takes no lines") == 1
    assert window.lines == []
    # The selection ends: the window shows what waited (the rest was dropped), then new lines.
    window.held.clear()
    window.free.set()
    deadline = time.monotonic() + 5
    while len(window.lines) < program.WINDOW_LINES:
        assert time.monotonic() < deadline, "the window did not show what waited"
        time.sleep(0.01)
    log.info("the fast lane is back")
    deadline = time.monotonic() + 5
    while not any("the fast lane is back" in line for line in window.lines):
        assert time.monotonic() < deadline, "the window got no line after it was free"
        time.sleep(0.01)
    assert len(window.lines) <= program.WINDOW_LINES + 2  # the rest was dropped, not kept


def test_before_bridge_12_a_held_window_stopped_every_thread_that_wrote(tmp_path: Path) -> None:
    """What happened on 2026-11-17, with logging as bridges 1 to 11 set it up."""
    import logging

    window = HeldWindow()
    log = logging.getLogger("held-window-before-12")
    log.propagate = False
    log.addHandler(logging.StreamHandler(window))
    try:
        stopped = threading.Thread(target=lambda: log.warning("a fast lane failed"), daemon=True)
        stopped.start()
        stopped.join(timeout=0.5)
        assert stopped.is_alive()  # waits for the window, for as long as it is held
    finally:
        window.held.clear()
        window.free.set()
        stopped.join(timeout=5)
        for h in list(log.handlers):
            log.removeHandler(h)
