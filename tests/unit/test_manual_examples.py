"""Manual Trading's calculator on HFM's own gold contract (S114): worked examples, each figure
checked by hand.

The contract is the one HFM's MT5 reported for XAUUSD on 2026-11-22 (terminal 1): 100 ounces a
lot, price step 0.01 worth 1.00 USD a lot (so 1.00 of price is 100 USD a lot), lots from 0.01
in steps of 0.01 up to 100, no minimum stop distance, swap in points (long -73.56, short 0.0),
three nights charged on Wednesday. The quote is Friday's close: bid 3538.14, ask 3538.59.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

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
