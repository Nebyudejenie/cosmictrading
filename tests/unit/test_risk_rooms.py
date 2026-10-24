"""The rooms a new buy has before a risk check would refuse it (S36): R05, R06, R07 and R19,
with the checks' own arithmetic, so the auto-trader sizes a trade smaller instead."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from cosmic.trading.policy import DEFAULT_POLICY
from cosmic.trading.risk import rooms
from cosmic.trading.risk.context import AccountState, OpenOrderState, PositionState

D = Decimal
PAXG = "SPOT:PAXG/USDT@BINANCE"


def state(**kw: Any) -> AccountState:
    base: dict[str, Any] = {
        "account_id": "a",
        "alias": "paper-100",
        "kind": "PAPER",
        "status": "ACTIVE",
        "venue_key": "BINANCE",
        "reconciliation_status": "OK",
        "last_reconciled_at": None,
        "cash_available": D(100),
        "positions": (),
        "open_orders": (),
        "unknown_orders": 0,
        "equity": D(100),
        "high_water_mark": D(100),
        "realized_pnl_today": D(0),
        "unrealized_pnl": D(0),
        "loss_streak": 0,
    }
    return AccountState(**{**base, **kw})


def room(**kw: Any) -> rooms.Room:
    return rooms.buy_room(
        state(**kw), DEFAULT_POLICY, instrument_key=PAXG, venue_key="BINANCE", limit_price=D(4000)
    )


def test_at_100_the_policy_allows_half_the_money_in_one_market_and_3_pct_at_risk() -> None:
    r = room()
    assert r.notional == D(50) and "50 % of equity in one market" in r.binding
    assert r.loss == D(3)


def test_open_trades_use_up_the_rooms() -> None:
    held = PositionState(PAXG, D("0.01"), D(4000))  # 40 held
    stop = OpenOrderState(PAXG, "SELL", D("0.01"), D(3800), True, purpose="STOP_LOSS")
    s = state(positions=(held,), open_orders=(stop,), cash_available=D(60))
    assert rooms.open_risk(s) == D(2)  # 0.01 × (4000 − 3800)
    r = room(positions=(held,), open_orders=(stop,), cash_available=D(60))
    assert r.notional == D(10)  # 50 − 40 held
    assert r.loss == D(1)  # 3 − 2


def test_free_cash_with_fees_can_bind() -> None:
    r = room(cash_available=D(20))
    assert r.binding == "the free cash, fees included"
    assert r.notional == (D(20) / (1 + D(60) / D(10_000)))


def test_unknown_marks_leave_the_loss_room_unknown_for_the_check_to_fail_closed() -> None:
    unmarked = PositionState(PAXG, D("0.01"), None)
    r = room(positions=(unmarked,))
    assert r.loss is None and r.notional is not None
    assert rooms.open_risk(state(positions=None)) is None
