"""The Windows MT5 bridge against the in-process MT5 emulator (ADR-028)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from bridge.cosmic_mt5_bridge import Bridge, Config, Journal, magic

from cosmic.trading.brokers.mt5_bridge import magic as server_magic
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
    assert bridge.run_command(command("c2", "PING", emu))["result"] == {"demo": False}
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
        assert r["result"] == {
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
