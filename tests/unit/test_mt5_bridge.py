"""The Windows MT5 bridge against the in-process MT5 emulator (ADR-028)."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bridge.cosmic_mt5_bridge import VERSION as BRIDGE_VERSION
from bridge.cosmic_mt5_bridge import Bridge, Config, Journal, magic, serve

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
    """Version 8 as it runs on the bridge computer (``serve``): the report and the fast lane
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

    def lane(payload: dict[str, Any]) -> dict[str, Any]:
        asked.append(json.loads(json.dumps(payload)))
        if payload["results"]:
            seen["result"] = payload["results"][0]
        time.sleep(0.02)  # the server's hold
        if len(reports) >= 2 and orders:  # while the second report's request is in flight
            seen["handed"] = time.monotonic()
            return {"commands": [orders.pop()]}
        return {"commands": []}

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
            done = "result" in seen and any(r.get("commands_done", [0, 0])[1] >= 1 for r in reports)
            if done:
                break
            time.sleep(0.02)
        stop.set()

    threading.Thread(target=enough, name="test-watch").start()
    serve(bridge, lane, stop)  # this thread is the MT5 thread
    assert watched.threads == {threading.current_thread().name}  # no other thread called MT5
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
