from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cosmic.core.labels import DataLabel
from cosmic.trading.modes import Mode
from cosmic.trading.policy import BlackoutWindow, VenueFee
from cosmic.trading.risk import checks
from cosmic.trading.risk.checks import Status
from cosmic.trading.risk.context import Effect, OpenOrderState, PositionState, Stage
from cosmic.trading.risk.engine import evaluate
from tests.risk_factory import (
    KEY,
    NOW,
    POLICY,
    account,
    authorization,
    context,
    held,
    instrument,
    market,
    open_order,
    request,
)

D = Decimal


def status_of(code: str, decision: object) -> Status:
    return next(r.status for r in decision.results if r.code == code)  # type: ignore[attr-defined]


def test_baseline_passes_all_checks() -> None:
    d = evaluate(context(), POLICY)
    assert d.passed, d.plain_summary()
    assert [r.code for r in d.results] == [f"R{i:02d}" for i in (*range(1, 17), 18, 19)]
    sub = evaluate(context(Stage.PRE_SUBMISSION), POLICY)
    assert sub.passed, sub.plain_summary()


@pytest.mark.parametrize(
    ("mode", "ok"),
    [
        (Mode.RESEARCH_ONLY, False),
        (Mode.PAPER_TRADING, True),
        (Mode.PROTECTION_ONLY, False),
        (Mode.EMERGENCY_STOP, False),
        (Mode.APPROVAL_REQUIRED, True),
    ],
)
def test_r01_mode_for_paper_account(mode: Mode, ok: bool) -> None:
    assert evaluate(context(mode=mode), POLICY).passed is ok


def test_r01_live_account_never_in_paper_or_research() -> None:  # AT-01, AT-02
    for mode in (Mode.RESEARCH_ONLY, Mode.PAPER_TRADING):
        d = evaluate(context(mode=mode, account=account(kind="LIVE")), POLICY)
        assert status_of("R01", d) is Status.FAIL


SCENARIOS: list[tuple[str, dict[str, object], str, Status]] = [
    ("disabled account", {"account": account(status="DISABLED")}, "R02", Status.FAIL),
    ("alias mismatch", {"request": request(account_alias="other")}, "R02", Status.FAIL),
    (
        "reconciliation required",
        {"account": account(reconciliation_status="REQUIRED")},
        "R03",
        Status.FAIL,
    ),
    (
        "reconciliation stale",
        {"account": account(last_reconciled_at=NOW - timedelta(hours=2))},
        "R03",
        Status.UNKNOWN,
    ),
    ("unknown orders", {"account": account(unknown_orders=1)}, "R03", Status.FAIL),
    ("cash unknown", {"account": account(cash_available=None)}, "R03", Status.UNKNOWN),
    (
        "not allowlisted",
        {
            "instrument": instrument(canonical_key="SPOT:DOGE/USD@COINBASE"),
            "request": request(instrument_key="SPOT:DOGE/USD@COINBASE"),
        },
        "R04",
        Status.FAIL,
    ),
    ("reference-only", {"instrument": instrument(tradable=False)}, "R04", Status.FAIL),
    ("market order", {"request": request(order_type="MARKET")}, "R04", Status.FAIL),
    ("short sale", {"request": request(side="SELL", effect=Effect.OPEN)}, "R04", Status.FAIL),
    ("notional too big", {"request": request(quantity=D("0.01"))}, "R05", Status.FAIL),
    ("position cap", {"account": account(positions=held("0.009"))}, "R06", Status.FAIL),
    (
        "concentration",
        {"account": account(equity=D("500"), high_water_mark=D("500"))},
        "R06",
        Status.FAIL,
    ),
    ("equity unknown", {"account": account(equity=None)}, "R06", Status.UNKNOWN),
    ("insufficient cash", {"account": account(cash_available=D("100"))}, "R07", Status.FAIL),
    (
        "no mark for holding",
        {"account": account(positions=held("0.001", mark=None))},
        "R07",
        Status.UNKNOWN,
    ),
    (
        "daily loss",
        {"account": account(realized_pnl_today=D("-80"), unrealized_pnl=D("-25"))},
        "R08",
        Status.FAIL,
    ),
    (
        "drawdown",
        {"account": account(equity=D("9000"), high_water_mark=D("10000"))},
        "R08",
        Status.FAIL,
    ),
    ("loss streak", {"account": account(loss_streak=4)}, "R08", Status.FAIL),
    ("pnl unknown", {"account": account(unrealized_pnl=None)}, "R08", Status.UNKNOWN),
    ("stale quote", {"market": market(quote_label=DataLabel.STALE)}, "R09", Status.FAIL),
    ("unknown quote", {"market": market(quote_label=DataLabel.UNKNOWN)}, "R09", Status.FAIL),
    (
        "old quote within label",
        {"market": market(quote_ts=NOW - timedelta(seconds=300))},
        "R09",
        Status.FAIL,
    ),
    ("halted", {"instrument": instrument(status="HALTED")}, "R10", Status.FAIL),
    ("venue degraded", {"market": market(venue_status="DEGRADED")}, "R10", Status.FAIL),
    (
        "venue status stale",
        {"market": market(venue_status_label=DataLabel.STALE)},
        "R10",
        Status.UNKNOWN,
    ),
    ("wide spread", {"market": market(bid=D("82700"), ask=D("83000"))}, "R10", Status.FAIL),
    ("price outside band", {"market": market(bid=D("84000"), ask=D("84001"))}, "R10", Status.FAIL),
    ("venues disagree", {"market": market(deviation_bps=D("80"))}, "R10", Status.FAIL),
    (
        "no cross-check",
        {"market": market(deviation_bps=None, deviation_label=DataLabel.STALE)},
        "R10",
        Status.UNKNOWN,
    ),
    ("slippage too wide", {"request": request(max_slippage_bps=D("50"))}, "R11", Status.FAIL),
    ("duplicate order", {"account": account(open_orders=(open_order(),))}, "R12", Status.FAIL),
    (
        "pending proposal",
        {"account": account(active_intents_same_instrument_side=1)},
        "R12",
        Status.FAIL,
    ),
    (
        "off-grid price",
        {"request": request(limit_price=D("83000.005"), price_band_high=D("83500"))},
        "R16",
        Status.FAIL,
    ),
    ("below min notional", {"instrument": instrument(min_notional=D("1000"))}, "R16", Status.FAIL),
    ("unknown lot size", {"instrument": instrument(lot_size=None)}, "R16", Status.UNKNOWN),
]


@pytest.mark.parametrize(
    ("name", "changes", "code", "expected"), SCENARIOS, ids=[s[0] for s in SCENARIOS]
)
def test_each_check_blocks(
    name: str, changes: dict[str, object], code: str, expected: Status
) -> None:
    d = evaluate(context(**changes), POLICY)  # type: ignore[arg-type]
    assert status_of(code, d) is expected, d.plain_summary()
    assert not d.passed
    assert code in d.plain_summary()


def test_r13_blackout() -> None:
    policy = POLICY.model_copy(
        update={
            "blackout_windows": (
                BlackoutWindow(
                    start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1), reason="FOMC"
                ),
            )
        }
    )
    d = evaluate(context(), policy)
    assert status_of("R13", d) is Status.FAIL and "FOMC" in d.plain_summary()


def test_r15_security_incident_blocks() -> None:
    d = evaluate(context(critical_incident_categories=frozenset({"SECURITY"})), POLICY)
    assert status_of("R15", d) is Status.FAIL
    assert evaluate(context(critical_incident_categories=frozenset({"DATA"})), POLICY).passed


@pytest.mark.parametrize(
    "auth_changes",
    [
        {"approval_status": "EXPIRED"},
        {"approval_status": "REVOKED"},
        {"approval_status": None},
        {"approval_expires_at": NOW - timedelta(seconds=1)},
        {"intent_expires_at": NOW},
        {"packet_hash_matches": False},
        {"policy_version_matches": False},
        {"operator_can_approve": False},
    ],
)
def test_r14_authorization_at_submission(auth_changes: dict[str, object]) -> None:  # AT-05, AT-07
    d = evaluate(context(Stage.PRE_SUBMISSION, authorization=authorization(**auth_changes)), POLICY)
    assert status_of("R14", d) is Status.FAIL and not d.passed


def test_r14_missing_authorization_record() -> None:
    d = evaluate(replace(context(Stage.PRE_SUBMISSION), authorization=None), POLICY)
    assert status_of("R14", d) is Status.FAIL


def test_reduce_only_path() -> None:
    reduce = request(side="SELL", effect=Effect.REDUCE, quantity=D("0.002"))
    ok = evaluate(context(request=reduce, account=account(positions=held("0.004"))), POLICY)
    assert ok.passed and [r.code for r in ok.results] == ["R01", "R02", "R16", "R17"]
    # Allowed even in EMERGENCY_STOP / PROTECTION_ONLY with stale data and no reconciliation
    for mode in (Mode.EMERGENCY_STOP, Mode.PROTECTION_ONLY):
        d = evaluate(
            context(
                mode=mode,
                request=reduce,
                market=market(quote_label=DataLabel.STALE),
                account=account(positions=held("0.004"), reconciliation_status="REQUIRED"),
            ),
            POLICY,
        )
        assert d.passed
    over = evaluate(
        context(
            request=reduce,
            account=account(
                positions=held("0.004"),
                open_orders=(open_order("SELL", "0.003", "90000", reduce_only=True),),
            ),
        ),
        POLICY,
    )
    assert status_of("R17", over) is Status.FAIL
    unknown = evaluate(context(request=reduce, account=account(positions=None)), POLICY)
    assert status_of("R17", unknown) is Status.UNKNOWN and not unknown.passed


def test_broken_check_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object) -> object:
        raise RuntimeError("bug")

    monkeypatch.setattr(checks, "EXPOSURE_CHECKS", (checks.r01_mode, boom))
    from cosmic.trading.risk import engine

    monkeypatch.setattr(engine, "EXPOSURE_CHECKS", (checks.r01_mode, boom))
    d = evaluate(context(), POLICY)
    assert not d.passed and any("could not be evaluated" in r.message for r in d.results)


def test_stale_data_blocks_exposure_at14() -> None:
    for label in (DataLabel.STALE, DataLabel.UNKNOWN, DataLabel.UNAVAILABLE, DataLabel.SIMULATED):
        assert not evaluate(context(market=market(quote_label=label)), POLICY).passed


def test_delayed_data_not_acceptable_for_live_accounts() -> None:
    d = evaluate(context(mode=Mode.APPROVAL_REQUIRED, account=account(kind="LIVE")), POLICY)
    assert status_of("R09", d) is Status.FAIL


def test_test_network_accounts_are_checked_like_paper() -> None:
    """SANDBOX (ADR-026): virtual funds, so PAPER_TRADING permits it and delayed data is
    acceptable, exactly as for paper; the venue check still applies."""
    d = evaluate(context(account=account(kind="SANDBOX")), POLICY)
    assert status_of("R01", d) is Status.PASS and status_of("R09", d) is Status.PASS


@settings(max_examples=150, deadline=None)
@given(
    limit=st.decimals(min_value=D("1"), max_value=D("5000"), places=2),
    tighter=st.decimals(min_value=D("0.01"), max_value=D("1"), places=2),
    qty=st.decimals(min_value=D("0.0001"), max_value=D("0.05"), places=4),
)
def test_tighter_limits_never_unblock(limit: Decimal, tighter: Decimal, qty: Decimal) -> None:
    """Monotonicity: lowering max_order_notional can turn PASS into FAIL, never FAIL into PASS."""
    loose = POLICY.model_copy(update={"max_order_notional": limit})
    tight = POLICY.model_copy(update={"max_order_notional": limit * tighter})
    ctx = context(request=request(quantity=qty))
    if not evaluate(ctx, loose).passed:
        assert not evaluate(ctx, tight).passed


def test_costs_and_cash_use_the_venues_own_fee() -> None:
    """R11 and R07 charge the owner's fee for the instrument's venue (B-08), else the general
    assumption."""
    own = POLICY.model_copy(update={"venue_fees": (VenueFee(venue="COINBASE", bps=D("10")),)})
    general = next(r for r in evaluate(context(), POLICY).results if r.code == "R11")
    venue = next(r for r in evaluate(context(), own).results if r.code == "R11")
    assert f"fee {POLICY.fee_bps_assumption} ASSUMPTION" in general.message
    assert "fee 10 ASSUMPTION" in venue.message
    assert venue.detail["estimated_cost_bps"] < general.detail["estimated_cost_bps"]
    other = POLICY.model_copy(update={"venue_fees": (VenueFee(venue="BINANCE", bps=D("10")),)})
    assert next(r for r in evaluate(context(), other).results if r.code == "R11").message == (
        general.message
    )  # another venue's fee never applies


def _result(code: str, ctx: object, policy: object = POLICY) -> object:
    return next(r for r in evaluate(ctx, policy).results if r.code == code)  # type: ignore[arg-type,attr-defined]


def test_r18_counts_positions_and_resting_buys_but_not_dust() -> None:
    three = tuple(
        PositionState(f"SPOT:{c}/USD@COINBASE", D("1"), D("100"), D("0.01"))
        for c in ("ETH", "SOL", "XRP")
    )
    full = context(account=account(positions=three))
    assert status_of("R18", evaluate(full, POLICY)) is Status.FAIL  # a 4th instrument
    more = context(
        account=account(positions=(*three[:2], PositionState(KEY, D("0.001"), D("83000"))))
    )
    assert status_of("R18", evaluate(more, POLICY)) is Status.PASS  # adds to one already held
    dust = tuple(replace(p, quantity=D("0.001")) for p in three)  # below one lot (0.01)
    assert status_of("R18", evaluate(context(account=account(positions=dust)), POLICY)) is (
        Status.PASS
    )
    resting = context(
        account=account(
            positions=three[:2],
            open_orders=(open_order(key="SPOT:ADA/USD@COINBASE", qty="1", price="1"),),
        )
    )
    assert status_of("R18", evaluate(resting, POLICY)) is Status.FAIL  # the resting buy counts


def test_r19_counts_the_loss_if_every_stop_is_hit() -> None:
    # 0.1 BTC held at 83,000 with a stop-limit at 80,500: 250 at risk; this trade adds 8.40.
    stop = OpenOrderState(KEY, "SELL", D("0.1"), D("80500"), True, "STOP_LOSS")
    protected = context(account=account(positions=held("0.1"), open_orders=(stop,)))
    r = _result("R19", protected)
    assert r.status is Status.PASS and r.detail["open_risk"] == D("258.4")  # 2.58 % of equity
    assert "loss if every stop is hit" in r.message
    lower = replace(stop, limit_price=D("80000"))  # 300 + 8.40 = 3.08 %: over the 3 % limit
    tight = context(account=account(positions=held("0.1"), open_orders=(lower,)))
    assert status_of("R19", evaluate(tight, POLICY)) is Status.FAIL


def test_r19_blocks_unstopped_holdings_and_trades_without_a_stop() -> None:
    bare = context(account=account(positions=held("0.1")))  # 8,300 at risk: no stop
    assert status_of("R19", evaluate(bare, POLICY)) is Status.FAIL
    no_stop = context(request=request(stop_limit_price=None))  # 332 = 3.32 % of 10,000
    r = _result("R19", no_stop)
    assert r.status is Status.FAIL and "3.32% of equity" in r.message
    small = context(request=request(stop_limit_price=None, quantity=D("0.003")))  # 2.49 %
    assert status_of("R19", evaluate(small, POLICY)) is Status.PASS
    entry = OpenOrderState(KEY, "BUY", D("0.004"), D("83000"), False, "ENTRY", "i1", None)
    rest = context(account=account(open_orders=(entry,)))  # a resting entry without a stop
    assert status_of("R19", evaluate(rest, POLICY)) is Status.FAIL  # 332 + 8.40 > 300
    with_stop = replace(entry, stop_limit_price=D("80900"))
    assert status_of(
        "R19", evaluate(context(account=account(open_orders=(with_stop,))), POLICY)
    ) is (Status.PASS)
    unmarked = context(account=account(positions=held("0.1", mark=None)))
    assert status_of("R19", evaluate(unmarked, POLICY)) is Status.UNKNOWN
    assert not evaluate(unmarked, POLICY).passed  # unknown never passes for new exposure


def test_the_new_limits_are_owner_settings() -> None:
    looser = POLICY.model_copy(update={"max_open_risk_pct": D("90")})
    bare = context(account=account(positions=held("0.1")))
    assert status_of("R19", evaluate(bare, looser)) is Status.PASS
    wider = POLICY.model_copy(update={"max_open_positions": 10})
    three = tuple(
        PositionState(f"SPOT:{c}/USD@COINBASE", D("1"), D("100")) for c in ("ETH", "SOL", "XRP")
    )
    assert status_of("R18", evaluate(context(account=account(positions=three)), wider)) is (
        Status.PASS
    )
