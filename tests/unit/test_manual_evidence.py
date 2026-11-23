"""What is verified about HFM's gold contract, and what is not (S115): each figure labelled from
HFM's own bookings, its own calculator or only its stated terms. The deals are made up for each
case; the contract is the one HFM reported for XAUUSD on 2026-11-22."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from cosmic.manual.calc import commission_of
from cosmic.manual.evidence import (
    CONTRADICTED,
    MEASURED,
    NOT_REPORTED,
    REPORTED,
    VERIFIED,
    Spreads,
    facts,
    nights,
)
from cosmic.manual.live import Deal, Quote
from tests.unit.test_manual_examples import QUOTE, SPEC

TUESDAY = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)  # server clock, as HFM stamps its deals
HISTORY = Spreads(101_276, D("0.34"), D("0.36"), D("0.45"), D("0.65"), 30)


def _trade(
    pid: int, side: str, entry: str, exit_: str, lots: str, *, held: timedelta, swap: str = "0"
) -> list[Deal]:
    opened, closed = TUESDAY, TUESDAY + held
    sign = 1 if side == "BUY" else -1
    profit = (D(exit_) - D(entry)) * D(lots) * 100 * sign
    back = "SELL" if side == "BUY" else "BUY"
    return [
        Deal(pid * 10, pid, opened, opened, side, 0, D(lots), D(entry), D(0), D(0), D(0), D(0), 1),
        Deal(
            pid * 10 + 1,
            pid,
            closed,
            closed,
            back,
            1,
            D(lots),
            D(exit_),
            profit,
            D(0),
            D(swap),
            D(0),
            1,
        ),
    ]


def _by_item(deals: list[Deal], **kw: object) -> dict[str, object]:
    out = facts(
        kw.pop("spec", SPEC),  # type: ignore[arg-type]
        deals=deals,
        commission=commission_of(deals),
        currency="USD",
        leverage=1000,
        quote=kw.pop("quote", QUOTE),  # type: ignore[arg-type]
        spread_history=kw.pop("spread_history", HISTORY),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )
    return {f.item: f for f in out}


def test_hfms_rollovers_are_counted_on_its_own_clock_with_the_triple_night() -> None:
    assert nights(TUESDAY, TUESDAY + timedelta(hours=2), 3) == 0  # the same day
    assert nights(TUESDAY, TUESDAY + timedelta(days=1), 3) == 1  # Tuesday night
    assert nights(TUESDAY, TUESDAY + timedelta(days=2), 3) == 4  # and Wednesday's three
    friday = datetime(2026, 11, 13, 20, tzinfo=UTC)
    assert nights(friday, friday + timedelta(days=3), 3) == 1  # Friday night only, not weekend


def test_the_contract_is_verified_by_hfms_own_booked_profit() -> None:
    deals = _trade(1, "BUY", "3500.00", "3512.34", "0.03", held=timedelta(hours=3))
    deals += _trade(2, "SELL", "3520.00", "3511.00", "0.10", held=timedelta(hours=1))
    f = _by_item(deals)
    assert f["Contract size"].status == VERIFIED  # type: ignore[attr-defined]
    assert "on 2 closed trades equals" in f["Contract size"].evidence  # type: ignore[attr-defined]
    assert f["Value of a price move"].status == VERIFIED  # type: ignore[attr-defined]
    assert "HFM filled 2 orders on this grid" in f["Lot sizes"].evidence  # type: ignore[attr-defined]
    wrong = list(deals)
    wrong[1] = replace(wrong[1], profit=D("30.00"))  # booked 30.00 where 37.02 was due
    assert _by_item(wrong)["Contract size"].status == CONTRADICTED  # type: ignore[attr-defined]
    assert _by_item([])["Contract size"].status == REPORTED  # type: ignore[attr-defined]


def test_a_swap_hfm_never_booked_is_contradicted_not_trusted() -> None:
    """The demo's own case (2026-11-23 probe): three BUYs held over HFM's rollover, booked with
    no swap at all, where its terms say -73.56 a lot a night."""
    deals = _trade(1, "BUY", "3500.00", "3501.00", "0.01", held=timedelta(days=1))
    f = _by_item(deals)
    buy = f["Swap, BUY"]
    assert buy.status == CONTRADICTED  # type: ignore[attr-defined]
    assert "HFM booked 0.00 on 1 overnight positions (1 nights)" in buy.evidence  # type: ignore[attr-defined]
    assert "terms give -0.74" in buy.evidence  # type: ignore[attr-defined]
    assert f["Swap, SELL"].status == REPORTED  # type: ignore[attr-defined]  # never held overnight
    paid = _trade(1, "BUY", "3500.00", "3501.00", "0.01", held=timedelta(days=1), swap="-0.7356")
    assert _by_item(paid)["Swap, BUY"].status == VERIFIED  # type: ignore[attr-defined]


def test_commission_and_spread_are_measured_and_say_where_from() -> None:
    deals = _trade(1, "BUY", "3500.00", "3501.00", "0.01", held=timedelta(hours=1))
    f = _by_item(deals)
    c = f["Commission"]
    assert (c.status, c.value) == (MEASURED, "0.00 a lot, both sides")  # type: ignore[attr-defined]
    assert "on this account only; another account type" in c.evidence  # type: ignore[attr-defined]
    s = f["Spread"]
    assert s.status == MEASURED and s.value == "median 0.34; 0.45 now"  # type: ignore[attr-defined]
    assert "99 % at or below 0.45, widest 0.65" in s.evidence  # type: ignore[attr-defined]
    none = _by_item([], spread_history=None, quote=None)
    assert none["Commission"].status == NOT_REPORTED  # type: ignore[attr-defined]
    assert none["Spread"].status == NOT_REPORTED  # type: ignore[attr-defined]


def test_the_margin_is_verified_only_by_hfms_own_calculator() -> None:
    estimated = _by_item([])["Margin and leverage"]
    assert estimated.status == REPORTED  # type: ignore[attr-defined]
    assert "the margin is only estimated" in estimated.evidence  # type: ignore[attr-defined]
    own = replace(SPEC, margin_buy_1lot=D("353.86"), margin_buy_at=D("3538.59"), calc_mode=0)
    m = _by_item([], spec=own)["Margin and leverage"]
    assert m.status == VERIFIED and "the leverage formula agrees" in m.evidence  # type: ignore[attr-defined]
    gold_rate = replace(own, margin_buy_1lot=D("7077.18"))  # HFM asking twice the formula's
    g = _by_item([], spec=gold_rate)["Margin and leverage"]
    assert "the leverage formula would give 353.86: HFM's is used" in g.evidence  # type: ignore[attr-defined]


def test_figures_an_older_bridge_does_not_read_are_not_reported_never_assumed() -> None:
    f = _by_item([])
    assert f["Freeze level"].status == NOT_REPORTED  # type: ignore[attr-defined]
    assert f["Stop-out level"].status == NOT_REPORTED  # type: ignore[attr-defined]
    newer = _by_item(
        [],
        spec=replace(SPEC, freeze_level=0),
        stop_out=D(20),
        stop_out_mode="PERCENT",
        margin_call=D(50),
    )
    assert newer["Freeze level"].value == "none"  # type: ignore[attr-defined]
    so = newer["Stop-out level"]
    assert (so.status, so.value) == (REPORTED, "20 %")  # type: ignore[attr-defined]
    assert "margin call at 50 %" in so.evidence  # type: ignore[attr-defined]
    assert (
        facts(
            None,
            deals=[],
            commission=commission_of([]),
            currency=None,
            leverage=None,
            quote=Quote(D(1), D(2), TUESDAY, timedelta(0)),
            spread_history=None,
        )[0].status
        == NOT_REPORTED
    )
