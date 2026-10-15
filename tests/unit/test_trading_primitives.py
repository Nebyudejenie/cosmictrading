from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from cosmic.core.errors import ValidationFailedError
from cosmic.trading import ledger
from cosmic.trading.modes import DESCRIPTIONS, Mode, Operation, mode_permits, selectable_targets
from cosmic.trading.policy import DEFAULT_POLICY, LIMIT_SEMANTICS, RiskPolicyParams, policy_hash
from cosmic.trading.precision import InstrumentRulesView, normalise
from cosmic.trading.sizing import size_long

D = Decimal
RULES = InstrumentRulesView(
    "SPOT:BTC/USD@COINBASE", D("0.01"), D("0.00000001"), D("0.00000001"), None, D("1")
)


def test_mode_matrix() -> None:
    assert not mode_permits(Mode.RESEARCH_ONLY, Operation.OPEN_EXPOSURE, account_kind="PAPER")[0]
    assert mode_permits(Mode.PAPER_TRADING, Operation.OPEN_EXPOSURE, account_kind="PAPER")[0]
    assert not mode_permits(Mode.PAPER_TRADING, Operation.OPEN_EXPOSURE, account_kind="LIVE")[0]
    assert not mode_permits(Mode.EMERGENCY_STOP, Operation.OPEN_EXPOSURE, account_kind="PAPER")[0]
    # SANDBOX (virtual funds on a test network, ADR-026) is authorised exactly like paper.
    for m in Mode:
        assert (
            mode_permits(m, Operation.OPEN_EXPOSURE, account_kind="SANDBOX")[0]
            == mode_permits(m, Operation.OPEN_EXPOSURE, account_kind="PAPER")[0]
        )
    assert (
        "test-network"
        in mode_permits(Mode.PAPER_TRADING, Operation.OPEN_EXPOSURE, account_kind="SANDBOX")[1]
    )
    for m in Mode:
        assert mode_permits(m, Operation.CANCEL, account_kind="PAPER")[0]
        assert mode_permits(m, Operation.REDUCE_EXPOSURE, account_kind="PAPER")[0]


def test_selectable_targets_offer_only_reachable_modes() -> None:
    cap = Mode.PAPER_TRADING
    assert selectable_targets(Mode.RESEARCH_ONLY, cap=cap, live_available=False) == [
        Mode.PAPER_TRADING,
        Mode.PROTECTION_ONLY,
    ]
    assert selectable_targets(Mode.PAPER_TRADING, cap=cap, live_available=False) == [
        Mode.RESEARCH_ONLY,
        Mode.PROTECTION_ONLY,
    ]
    # Live preconditions met but the deployment cap is still paper: nothing live is offered.
    assert not set(selectable_targets(Mode.PAPER_TRADING, cap=cap, live_available=True)) & {
        Mode.APPROVAL_REQUIRED,
        Mode.STRATEGY_AUTHORIZED,
    }
    assert Mode.APPROVAL_REQUIRED in selectable_targets(
        Mode.PAPER_TRADING, cap=Mode.STRATEGY_AUTHORIZED, live_available=True
    )
    assert Mode.EMERGENCY_STOP not in selectable_targets(
        Mode.PROTECTION_ONLY, cap=cap, live_available=False
    )
    assert set(DESCRIPTIONS) == set(Mode)


def test_default_policy_valid_and_semantics_documented() -> None:
    assert DEFAULT_POLICY.allowed_order_types == ("LIMIT",)
    assert DEFAULT_POLICY.allow_short is False and DEFAULT_POLICY.max_leverage == 1
    assert DEFAULT_POLICY.emergency.liquidate_positions is False
    for key in (
        "max_order_notional",
        "max_position_notional",
        "max_gross_exposure",
        "max_concentration_pct",
        "max_daily_loss",
        "max_drawdown_pct",
        "max_loss_streak",
    ):
        sem = LIMIT_SEMANTICS[key]
        assert {"measure", "currency", "boundary", "fees", "unrealised", "unavailable"} <= set(sem)
        assert sem["unavailable"] == "block new exposure"
    assert policy_hash(DEFAULT_POLICY) == policy_hash(
        RiskPolicyParams.model_validate(DEFAULT_POLICY.model_dump(mode="python"))
    )


@pytest.mark.parametrize(
    "change",
    [
        {"max_leverage": "2"},
        {"allow_short": True},
        {"allowed_order_types": ["MARKET"]},
        {"max_order_notional": "5000"},
        {"unexpected_field": 1},
        {"approval_ttl_seconds": 99999},
        {"emergency": {"liquidate_positions": True}},
        {"max_concentration_pct": "150"},
    ],
)
def test_policy_rejects_unsafe_values(change: dict[str, object]) -> None:
    data = DEFAULT_POLICY.model_dump(mode="python")
    data.update(change)
    with pytest.raises(ValidationError):
        RiskPolicyParams.model_validate(data)


def test_normalise_examples() -> None:
    n = normalise(RULES, "BUY", D("0.0040000099"), D("83000.019"), D("10"))
    assert n.ok and n.quantity == D("0.00400000") and n.limit_price == D("83000.01")
    s = normalise(RULES, "SELL", D("0.004"), D("83000.011"), D("10"))
    assert s.limit_price == D("83000.02")  # sells round up: never sell for less than asked
    coarse = InstrumentRulesView("X", D("0.01"), D("0.001"), D("0.001"), None, D("10"))
    bad = normalise(coarse, "BUY", D("0.0019"), D("83000"), D("10"))
    assert not bad.ok and any("rounding" in p for p in bad.problems)
    tiny = normalise(coarse, "BUY", D("0.0001"), D("83000"), D("10"))
    assert not tiny.ok


@settings(max_examples=200, deadline=None)
@given(
    qty=st.decimals(min_value=D("0.00001"), max_value=D("10"), places=10),
    price=st.decimals(min_value=D("1"), max_value=D("200000"), places=4),
)
def test_normalise_never_increases_exposure(qty: Decimal, price: Decimal) -> None:
    n = normalise(RULES, "BUY", qty, price, D("10"))
    assert n.quantity <= qty
    assert n.limit_price <= price
    assert n.quantity * n.limit_price <= qty * price


@settings(max_examples=200, deadline=None)
@given(
    equity=st.decimals(min_value=D("100"), max_value=D("1000000"), places=2),
    entry=st.decimals(min_value=D("10"), max_value=D("100000"), places=2),
    stop_pct=st.decimals(min_value=D("0.5"), max_value=D("30"), places=1),
    cash=st.decimals(min_value=D("0"), max_value=D("100000"), places=2),
)
def test_sizing_respects_every_cap(
    equity: Decimal, entry: Decimal, stop_pct: Decimal, cash: Decimal
) -> None:
    stop = entry * (1 - stop_pct / 100)
    r = size_long(
        equity=equity,
        entry=entry,
        stop=stop,
        risk_per_trade_pct=D("1"),
        uncertainty_haircut_pct=D("25"),
        lot_size=D("0.00000001"),
        min_qty=None,
        min_notional=D("1"),
        max_order_notional=D("500"),
        position_headroom_notional=D("1000"),
        cash_available=cash,
        fee_bps=D("60"),
    )
    assert r.quantity >= 0
    assert r.quantity * entry <= D("500")
    assert r.quantity * entry * (1 + D("0.006")) <= cash or r.quantity == 0
    assert r.quantity * (entry - stop) <= equity * D("0.01")


def test_sizing_invalid_stop_and_minimums() -> None:
    kw = dict(
        equity=D("10000"),
        risk_per_trade_pct=D("1"),
        uncertainty_haircut_pct=D("25"),
        lot_size=D("0.001"),
        min_qty=D("0.001"),
        min_notional=D("10"),
        max_order_notional=D("500"),
        position_headroom_notional=D("1000"),
        cash_available=D("10000"),
        fee_bps=D("60"),
    )
    assert size_long(entry=D("100"), stop=D("100"), **kw).quantity == 0  # type: ignore[arg-type]
    tiny = size_long(entry=D("83000"), stop=D("82000"), **{**kw, "max_order_notional": D("50")})  # type: ignore[arg-type]
    assert tiny.quantity == 0 and "minimum" in tiny.notes[-1]


def test_fill_entries_balance_and_signs() -> None:
    buy = ledger.fill_entries(
        side="BUY",
        base="BTC",
        quote="USD",
        quantity=D("0.004"),
        price=D("83000"),
        fee=D("1.99"),
        fee_asset="USD",
    )
    ledger.check_balanced(buy)
    assert ledger.Entry("POSITION", "BTC", D("0.004")) in buy
    assert ledger.Entry("CASH", "USD", D("-332.000")) in buy
    sell = ledger.fill_entries(
        side="SELL",
        base="BTC",
        quote="USD",
        quantity=D("0.004"),
        price=D("84000"),
        fee=D("2"),
        fee_asset="USD",
    )
    ledger.check_balanced(sell)
    assert ledger.Entry("CASH", "USD", D("336.000")) in sell
    with pytest.raises(ValidationFailedError):
        ledger.check_balanced([ledger.Entry("CASH", "USD", D("1"))])
    with pytest.raises(ValidationFailedError):
        ledger.fill_entries(
            side="BUY",
            base="BTC",
            quote="USD",
            quantity=D("0"),
            price=D("1"),
            fee=D("0"),
            fee_asset="USD",
        )


def test_fees_come_out_of_the_asset_they_are_charged_in() -> None:
    """ADR-026 §4: a Binance buy pays commission in the base asset; other assets (BNB) come
    out of the unmanaged holdings."""
    base_fee = ledger.fill_entries(
        side="BUY",
        base="BTC",
        quote="USDT",
        quantity=D("0.004"),
        price=D("83000"),
        fee=D("0.000004"),
        fee_asset="BTC",
    )
    ledger.check_balanced(base_fee)
    assert ledger.Entry("POSITION", "BTC", D("-0.000004")) in base_fee
    assert ledger.Entry("FEES", "BTC", D("0.000004")) in base_fee
    bnb = ledger.fill_entries(
        side="BUY",
        base="BTC",
        quote="USDT",
        quantity=D("0.004"),
        price=D("83000"),
        fee=D("0.00001"),
        fee_asset="BNB",
    )
    ledger.check_balanced(bnb)
    assert ledger.Entry("UNMANAGED", "BNB", D("-0.00001")) in bnb


def test_only_whole_lots_are_sellable() -> None:
    from cosmic.trading.proposals import sellable

    assert sellable(D("0.003996"), D("0.00001")) == D("0.00399")
    assert sellable(D("0.000006"), D("0.00001")) == 0  # dust
    assert sellable(D("0.004"), D("0.00001")) == D("0.004")
    assert sellable(D("0.003996"), None) == D("0.003996")  # unknown lot: unchanged
