"""Manual Trading's calculator on HFM's own gold contract (S114): worked examples, each figure
checked by hand.

The contract is the one HFM's MT5 reported for XAUUSD on 2026-11-22 (terminal 1): 100 ounces a
lot, price step 0.01 worth 1.00 USD a lot (so 1.00 of price is 100 USD a lot), lots from 0.01
in steps of 0.01 up to 100, no minimum stop distance, swap in points (long -73.56, short 0.0),
three nights charged on Wednesday. The quote is Friday's close: bid 3538.14, ask 3538.59.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cosmic.manual.calc import (
    BLOCK,
    WARN,
    Ticket,
    commission_of,
    on_grid,
    size,
    swap_per_lot,
    triple_night,
    valid_lots,
)
from cosmic.manual.live import Deal, Quote, Spec, spec_of

HFM_XAUUSD = {  # as reported on 2026-11-22 (bridge snapshot, terminal 1)
    "point": "0.01",
    "digits": 2,
    "swap_long": "-73.56",
    "swap_mode": 1,
    "tick_size": "0.01",
    "swap_short": "0.0",
    "tick_value": "1.0",
    "volume_max": "100.0",
    "volume_min": "0.01",
    "stops_level": 0,
    "volume_step": "0.01",
    "contract_size": "100.0",
    "currency_margin": "USD",
    "currency_profit": "USD",
    "swap_rollover3days": 3,
}
SPEC = spec_of(HFM_XAUUSD)
assert SPEC is not None
NOW = datetime(2026, 11, 20, 21, 59, 59, tzinfo=UTC)
QUOTE = Quote(D("3538.14"), D("3538.59"), NOW, timedelta(seconds=1))


def _size(ticket: Ticket, equity: str, *, leverage: int = 1000, commission: str = "0"):  # type: ignore[no-untyped-def]
    return size(
        ticket,
        equity=D(equity),
        margin_free=D(equity),
        leverage=leverage,
        currency="USD",
        spec=SPEC,
        quote=QUOTE,
        max_risk_pct=D(2),
        commission_per_lot=D(commission),
    )


def test_the_contract_is_read_as_hfm_reports_it() -> None:
    assert SPEC.value_per_price == 100  # 1.00 of price, one lot: 100 USD
    assert (SPEC.volume_min, SPEC.volume_step, SPEC.volume_max) == (D("0.01"), D("0.01"), 100)
    assert QUOTE.spread == D("0.45")
    assert swap_per_lot(SPEC, "BUY", D("3538.59"), "USD") == D("-73.56")  # a lot, a night
    assert swap_per_lot(SPEC, "SELL", D("3538.14"), "USD") == 0
    assert triple_night(SPEC) == "Wednesday"


@pytest.mark.parametrize(
    ("equity", "side", "entry", "stop", "target", "risk", "leverage", "want"),
    [
        # 1 % of 1,000 is 10.00; a 10.00 stop loses 1,000 a lot: 0.01 lot exactly.
        (
            "1000",
            "BUY",
            "3538.59",
            "3528.59",
            "3558.59",
            "1",
            1000,
            {
                "lots": "0.01",
                "risk": "10.00",
                "pct": "1.00",
                "reward": "20.00",
                "rr": "2.00",
                "margin": "3.54",
                "spread": "0.45",
                "swap": "-0.74",
            },
        ),
        # 2 % of 5,000 is 100.00; a 15.00 stop loses 1,500 a lot: 0.0666 rounds DOWN to 0.06.
        (
            "5000",
            "SELL",
            "3538.14",
            "3553.14",
            "3508.14",
            "2",
            500,
            {
                "lots": "0.06",
                "risk": "90.00",
                "pct": "1.80",
                "reward": "180.00",
                "rr": "2.00",
                "margin": "42.46",
                "spread": "2.70",
                "swap": "0.00",
            },
        ),
        # 1 % of 100 is 1.00: only a 1.00 stop fits HFM's smallest trade (0.01 lot = 1 oz).
        (
            "100",
            "BUY",
            "3538.59",
            "3537.59",
            "3540.59",
            "1",
            1000,
            {
                "lots": "0.01",
                "risk": "1.00",
                "pct": "1.00",
                "reward": "2.00",
                "rr": "2.00",
                "margin": "3.54",
                "spread": "0.45",
                "swap": "-0.74",
            },
        ),
        # 1.5 % of 2,500 is 37.50; a 7.25 stop loses 725 a lot: 0.0517 rounds DOWN to 0.05.
        (
            "2500",
            "SELL",
            "3538.14",
            "3545.39",
            "3523.64",
            "1.5",
            1000,
            {
                "lots": "0.05",
                "risk": "36.25",
                "pct": "1.45",
                "reward": "72.50",
                "rr": "2.00",
                "margin": "17.69",
                "spread": "2.25",
                "swap": "0.00",
            },
        ),
        # A 1:1.5 plan: 1 % of 20,000 is 200.00; a 12.40 stop loses 1,240 a lot: 0.16 lot.
        (
            "20000",
            "BUY",
            "3538.59",
            "3526.19",
            "3557.19",
            "1",
            1000,
            {
                "lots": "0.16",
                "risk": "198.40",
                "pct": "0.99",
                "reward": "297.60",
                "rr": "1.50",
                "margin": "56.62",
                "spread": "7.20",
                "swap": "-11.77",
            },
        ),
    ],
)
def test_worked_examples_on_hfms_contract(
    equity: str,
    side: str,
    entry: str,
    stop: str,
    target: str,
    risk: str,
    leverage: int,
    want: dict[str, str],
) -> None:
    s = _size(
        Ticket(side, D(entry), D(stop), D(target), risk_pct=D(risk)), equity, leverage=leverage
    )
    assert not s.blocked, s.lines
    assert s.lots == D(want["lots"]) and on_grid(s.lots, SPEC)
    assert f"{s.risk_cash:.2f}" == want["risk"]  # the loss at the stop-loss
    assert f"{s.risk_pct:.2f}" == want["pct"]
    assert s.risk_cash <= D(equity) * D(risk) / 100  # never more than asked
    assert f"{s.reward_cash:.2f}" == want["reward"]  # the profit at the target
    assert f"{s.rr:.2f}" == want["rr"]
    assert f"{s.margin:.2f}" == want["margin"]
    assert f"{s.spread_cost:.2f}" == want["spread"]
    assert f"{s.swap_night:.2f}" == want["swap"]


def test_an_account_too_small_for_the_stop_is_no_trade_never_more_risk() -> None:
    """1 % of 100 is 1.00; a 20.00 stop loses 20.00 even at 0.01 lot: NO TRADE, with the
    equity at which it would fit, and the risk is never raised."""
    s = _size(Ticket("BUY", D("3538.59"), D("3518.59"), D("3578.59"), risk_pct=D(1)), "100")
    assert s.blocked and s.lots == 0
    (why,) = [x.text for x in s.lines if x.level == BLOCK]
    assert why.startswith("HFM's smallest trade (0.01 lot) would lose 20.00 at this stop-loss")
    assert "20.00 % of equity" in why and "NO TRADE" in why
    assert "fits from about 2,000 of equity" in why and "Never raise the risk" in why
    at_2000 = _size(Ticket("BUY", D("3538.59"), D("3518.59"), None, risk_pct=D(1)), "2000")
    assert at_2000.lots == D("0.01") and at_2000.risk_cash == 20  # exactly the boundary


def test_a_typed_lot_size_is_checked_never_trusted() -> None:
    plan = {"side": "BUY", "entry": D("3538.59"), "stop": D("3528.59"), "target": None}
    odd = _size(Ticket(**plan, lots=D("0.015")), "1000")
    assert odd.blocked and "0.015 lot is not a size HFM accepts" in odd.lines[-1].text
    too_big = _size(Ticket(**plan, lots=D("0.05")), "1000")  # 50.00 = 5 % of 1,000
    assert too_big.blocked and "above your limit of 2 % a trade" in too_big.lines[-1].text
    ok = _size(Ticket(**plan, lots=D("0.02")), "1000")  # 20.00 = 2 %: at the limit, allowed
    assert not ok.blocked and (ok.lots, ok.risk_cash) == (D("0.02"), D("20.00"))
    huge = _size(Ticket(**plan, lots=D("150")), "100000000")
    assert huge.blocked and "up to 100" in huge.lines[-1].text


def test_commission_is_measured_from_hfms_own_deals_and_counted_in_the_risk() -> None:
    def deal(entry: int, commission: str) -> Deal:
        return Deal(
            1,
            1,
            NOW,
            NOW,
            "BUY",
            entry,
            D("0.10"),
            D("3538.59"),
            D(0),
            D(commission),
            D(0),
            D(0),
            0,
        )

    measured = commission_of([deal(0, "-0.60"), deal(1, "-0.60")])  # 1.20 for 0.10 lot
    assert (measured.per_lot, measured.deals) == (D("12"), 2)  # 12.00 a lot, both sides
    assert commission_of([]).per_lot is None  # no deals yet: not known, never guessed
    # 1 % of 1,000 is 10.00; the 10.00 stop alone loses 10.00 at 0.01 lot: with 12.00 a lot
    # of commission it is 10.12, over the 10.00 allowed: NO TRADE.
    at_1000 = _size(
        Ticket("BUY", D("3538.59"), D("3528.59"), D("3558.59"), risk_pct=D(1)),
        "1000",
        commission="12",
    )
    assert at_1000.blocked and "would lose 10.12" in at_1000.lines[-1].text
    at_2000 = _size(
        Ticket("BUY", D("3538.59"), D("3528.59"), D("3558.59"), risk_pct=D(1)),
        "2000",
        commission="12",
    )
    assert (at_2000.lots, at_2000.risk_cash, at_2000.reward_cash) == (
        D("0.01"),
        D("10.12"),
        D("19.88"),
    )
    assert at_2000.commission == D("0.12")


def test_margin_beyond_the_free_margin_is_refused() -> None:
    """1 % of 1,000 with a 0.10 stop is 1.00 lot: about 3,538.59 of margin at 1:100."""
    s = _size(Ticket("BUY", D("3538.59"), D("3538.49"), None, risk_pct=D(1)), "1000", leverage=100)
    assert s.blocked and s.lines[-1].text.startswith("not affordable: it needs about 3538.59")
    assert any(x.level == WARN and "within two spreads" in x.text for x in s.lines)


def test_sizes_round_down_to_hfms_grid_from_its_minimum() -> None:
    other = Spec(
        "XAUUSD",
        D(100),
        D("0.01"),
        D(1),
        D("0.1"),
        D("0.05"),
        D(10),
        2,
        0,
        D("0.01"),
        None,
        None,
        "USD",
        "USD",
    )
    assert valid_lots(D("0.27"), other) == D("0.25")
    assert valid_lots(D("0.09"), other) == 0
    assert valid_lots(D("0.1"), other) == D("0.1")
    assert on_grid(D("0.15"), other) and not on_grid(D("0.12"), other)
    big = _size(Ticket("BUY", D("3538.59"), D("3537.59"), None, risk_pct=D(2)), "10000000")
    assert big.lots == 100 and any("HFM's largest trade" in x.text for x in big.lines)


# ---- S115: the calculator's evidence, case by case, on HFM's reported contract -------------------


def _sized(
    ticket: Ticket,
    equity: str,
    *,
    quote: Quote | None = QUOTE,
    spec: Spec = SPEC,
    margin_free: str | None = None,
    leverage: int = 1000,
    normal_spread: str | None = None,
    margin_used: str | None = None,
    stop_out: str | None = None,
):  # type: ignore[no-untyped-def]
    return size(
        ticket,
        equity=D(equity),
        margin_free=D(margin_free) if margin_free is not None else D(equity),
        leverage=leverage,
        currency="USD",
        spec=spec,
        quote=quote,
        max_risk_pct=D(2),
        normal_spread=D(normal_spread) if normal_spread else None,
        margin_used=D(margin_used) if margin_used else None,
        stop_out_pct=D(stop_out) if stop_out else None,
    )


@pytest.mark.parametrize(
    (
        "equity",
        "side",
        "entry",
        "stop",
        "target",
        "risk",
        "lots",
        "loss",
        "pct",
        "reward",
        "margin",
    ),
    [
        # 1 % of 500 = 5.00; a 5.00 stop loses 500 a lot: exactly 0.01 lot.
        (
            "500",
            "BUY",
            "3538.59",
            "3533.59",
            "3548.59",
            "1",
            "0.01",
            "5.00",
            "1.00",
            "10.00",
            "3.54",
        ),
        # 1 % of 3,000 = 30.00; a 4.00 stop loses 400 a lot: 0.075 rounds DOWN to 0.07 (28.00).
        (
            "3000",
            "SELL",
            "3538.14",
            "3542.14",
            "3530.14",
            "1",
            "0.07",
            "28.00",
            "0.93",
            "56.00",
            "24.77",
        ),
        # 2 % of 10,000 = 200.00; a 25.00 stop loses 2,500 a lot: exactly 0.08 lot.
        (
            "10000",
            "BUY",
            "3538.59",
            "3513.59",
            "3588.59",
            "2",
            "0.08",
            "200.00",
            "2.00",
            "400.00",
            "28.31",
        ),
        # 0.5 % of 50,000 = 250.00; a 7.77 stop loses 777 a lot: 0.3217 rounds DOWN to 0.32.
        (
            "50000",
            "SELL",
            "3538.14",
            "3545.91",
            "3522.60",
            "0.5",
            "0.32",
            "248.64",
            "0.50",
            "497.28",
            "113.22",
        ),
    ],
)
def test_more_worked_examples_buy_and_sell_at_other_stops_and_equities(
    equity: str, side: str, entry: str, stop: str, target: str, risk: str,
    lots: str, loss: str, pct: str, reward: str, margin: str,
) -> None:  # fmt: skip
    s = _sized(Ticket(side, D(entry), D(stop), D(target), risk_pct=D(risk)), equity)
    assert not s.blocked, s.lines
    assert (s.lots, f"{s.risk_cash:.2f}", f"{s.risk_pct:.2f}") == (D(lots), loss, pct)
    assert (f"{s.reward_cash:.2f}", f"{s.margin:.2f}") == (reward, margin)
    assert s.risk_cash <= D(equity) * D(risk) / 100
    assert s.margin_source.startswith("estimated from the leverage 1:1000")


@pytest.mark.parametrize(
    ("side", "entry", "stop", "target", "why"),
    [
        ("BUY", "3538.59", "3540.00", "3550.00", "a BUY's stop-loss must be below its entry"),
        ("SELL", "3538.14", "3530.00", "3520.00", "a SELL's stop-loss must be above its entry"),
        ("BUY", "3538.59", "3538.59", "3550.00", "a BUY's stop-loss must be below its entry"),
        ("SELL", "3538.14", "3538.14", "3520.00", "a SELL's stop-loss must be above its entry"),
        ("BUY", "3538.59", "3528.59", "3530.00", "a BUY's target must be above its entry"),
        ("SELL", "3538.14", "3548.14", "3540.00", "a SELL's target must be below its entry"),
        ("BUY", "3538.59", "0", "3550.00", "must be prices above 0"),
        ("SELL", "3538.14", "3548.14", "-5", "must be prices above 0"),
        ("HOLD", "3538.59", "3528.59", "3550.00", "choose BUY or SELL"),
    ],
)
def test_an_invalid_stop_loss_or_target_is_refused(
    side: str, entry: str, stop: str, target: str, why: str
) -> None:
    s = _sized(Ticket(side, D(entry), D(stop), D(target), risk_pct=D(1)), "10000")
    assert s.blocked and s.lots == 0 and s.risk_cash == 0
    assert any(x.level == BLOCK and why in x.text for x in s.lines)


def test_a_sell_whose_smallest_trade_risks_too_much_is_no_trade() -> None:
    """1 % of 250 is 2.50; HFM's smallest SELL with a 3.00 stop loses 3.00 (1.20 %)."""
    s = _sized(Ticket("SELL", D("3538.14"), D("3541.14"), D("3532.14"), risk_pct=D(1)), "250")
    assert s.blocked and s.lots == 0
    (why,) = [x.text for x in s.lines if x.level == BLOCK]
    assert "would lose 3.00 at this stop-loss, 1.20 % of equity" in why
    assert "fits from about 300 of equity" in why and "Never raise the risk" in why


def test_hfms_own_margin_figure_is_used_and_refuses_what_the_free_margin_cannot_pay() -> None:
    """Bridge 13 reports HFM's own margin for one lot; at 1:100 it is 3,538.59 a lot at the ask.
    1 % of 3,000 with a 3.00 stop is 0.10 lot: 353.86 of margin, more than 300 free."""
    own = replace(SPEC, margin_buy_1lot=D("3538.59"), margin_buy_at=D("3538.59"))
    t = Ticket("BUY", D("3538.59"), D("3535.59"), None, risk_pct=D(1))
    s = _sized(t, "3000", spec=own, margin_free="300")
    assert s.blocked and "it needs about 353.86 of margin and 300.00 is free" in s.lines[-1].text
    ok = _sized(t, "3000", spec=own, margin_free="1000")
    assert (ok.lots, f"{ok.margin:.2f}", ok.margin_source) == (
        D("0.10"),
        "353.86",
        "HFM's own margin calculator",
    )


def test_a_loss_to_the_stop_that_reaches_hfms_stop_out_is_refused() -> None:
    """Equity 1,000, 2 % at a 2.00 stop: 0.10 lot, 20.00 lost at the stop. With 2,000 of margin
    already in use, the margin level at the stop is 980 / 2,035.39 = 48 %: at or below a 50 %
    stop-out, HFM would close positions itself first."""
    t = Ticket("BUY", D("3538.59"), D("3536.59"), None, risk_pct=D(2))
    s = _sized(t, "1000", margin_free="5000", margin_used="2000", stop_out="50")
    assert s.blocked and "the margin level falls to 48 %" in s.lines[-1].text
    assert "HFM's stop-out of 50 %" in s.lines[-1].text
    calm = _sized(t, "1000", margin_used="100", stop_out="50")
    assert not calm.blocked and calm.margin_level_at_stop is not None
    assert f"{calm.margin_level_at_stop:.0f}" == "724"  # 980 / 135.39


def test_a_widened_spread_is_warned_and_a_sell_is_told_its_stop_is_hit_by_the_ask() -> None:
    wide = Quote(D("3537.54"), D("3538.74"), NOW, timedelta(seconds=1))  # spread 1.20
    s = _sized(
        Ticket("SELL", D("3537.54"), D("3547.54"), None, risk_pct=D(1)),
        "10000",
        quote=wide,
        normal_spread="0.34",
    )
    (warn,) = [x.text for x in s.lines if "times its usual" in x.text]
    assert warn.startswith("the spread is 1.20 now, 3.5 times its usual 0.34")
    assert "a SELL's stop-loss is hit by the ask" in warn
    assert s.spread_cost == s.lots * D("1.20") * 100  # the wider spread is paid
    usual = _sized(
        Ticket("SELL", D("3538.14"), D("3548.14"), None, risk_pct=D(1)),
        "10000",
        normal_spread="0.34",
    )
    assert not any("times its usual" in x.text for x in usual.lines)  # 0.45 is under 2 x 0.34


def test_without_a_current_price_the_size_is_given_but_the_missing_checks_are_said() -> None:
    """A stale or missing price reaches the calculator as none (the page passes only a fresh
    one): the size is still worked out from the typed entry, the spread is not counted, and the
    page's risk checks refuse the trade itself (tests/unit/test_manual_calc.py)."""
    s = _sized(Ticket("BUY", D("3538.59"), D("3528.59"), None, risk_pct=D(1)), "1000", quote=None)
    assert s.lots == D("0.01") and s.spread_cost is None
    assert any(x.level == WARN and x.text.startswith("no current HFM price") for x in s.lines)


@settings(max_examples=300, deadline=None)
@given(
    equity=st.decimals(min_value=D(50), max_value=D(1_000_000), places=2),
    risk=st.decimals(min_value=D("0.1"), max_value=D(2), places=2),
    distance=st.decimals(min_value=D("0.10"), max_value=D(60), places=2),
    buy=st.booleans(),
)
def test_the_size_is_always_on_hfms_grid_never_over_the_risk_and_the_largest_that_fits(
    equity: D, risk: D, distance: D, buy: bool
) -> None:
    """Whatever the equity, risk and stop: the lot size is on HFM's grid, its loss at the stop
    is never above the money allowed, and one step more would be (or HFM's largest trade is
    reached); a refusal happens only when HFM's smallest trade already risks too much."""
    entry = D("3538.59") if buy else D("3538.14")
    stop = entry - distance if buy else entry + distance
    s = _sized(Ticket("BUY" if buy else "SELL", entry, stop, None, risk_pct=risk), f"{equity}")
    allowed = equity * risk / 100
    per_lot = distance * 100
    if s.blocked:
        assert s.lots == 0 and SPEC.volume_min * per_lot > allowed
        return
    assert on_grid(s.lots, SPEC) and s.risk_cash == s.lots * per_lot
    assert s.risk_cash <= allowed
    assert s.lots == SPEC.volume_max or (s.lots + SPEC.volume_step) * per_lot > allowed
