"""The owner's PDF plan ($100 → $25,000, S117): every exact figure is the PDF's own, to the cent,
and the simulation reproduces the PDF's 20,000-run variance table within its sampling error."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from cosmic.manual import book, challenge, risk
from cosmic.manual.calc import BLOCK, Ticket
from cosmic.manual.live import BUY, ENTRY_IN, ENTRY_OUT, SELL

from .test_manual_calc import NOW, _account, _deal, _size

START = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("rr", "trades", "days", "balance", "ev", "breakeven"),
    [
        (D("1.5"), 150, 75, D("25015.88"), D("0.0375"), D("0.40")),
        (D(2), 114, 57, D("26036.35"), D("0.05"), D("0.3333")),
        (D(3), 72, 36, D("25498.25"), D("0.08"), D("0.25")),
    ],
)
def test_each_scenario_is_the_pdfs_to_the_cent(
    rr: D, trades: int, days: int, balance: D, ev: D, breakeven: D
) -> None:
    s = challenge.SCENARIOS[rr]
    assert challenge.trades_to(challenge.START, s) == trades == s.roadmap_trades
    assert challenge.trading_days(trades) == days
    assert challenge.expected_balance(challenge.START, trades, s) == balance
    assert s.ev == ev and round(s.breakeven, 4) == breakeven


def test_the_roadmap_rows_and_the_blow_up_counts_are_the_pdfs() -> None:
    s = challenge.SCENARIOS[D(2)]  # the PDF's page 12-16
    rows = {n: challenge.expected_balance(challenge.START, n, s) for n in (1, 10, 33, 66, 94, 113)}
    assert rows == {
        1: D("105.00"),
        10: D("162.89"),
        33: D("500.32"),
        66: D("2503.19"),
        94: D("9812.83"),
        113: D("24796.52"),
    }
    # "44 consecutive losses from $100, 66 from $1,000, 88 from $10,000 and 97 from $25,000"
    assert [challenge.blow_up_in(D(b)) for b in (100, 1000, 10000, 25000)] == [44, 66, 88, 97]
    assert challenge.blow_up_in(D("103.75")) == 45  # the 1:1.5 roadmap's trade 1
    nxt = challenge.next_trade(D("103.75"), challenge.SCENARIOS[D("1.5")])
    assert (nxt.risk, nxt.win) == (D("10.38"), D("15.57"))  # the PDF: $10.38 and +$15.56*
    # *the PDF takes 1.5 × the unrounded risk (15.5625 → 15.56); the page shows the rounded
    # risk's multiple, a cent apart at most.


def test_the_streak_table_is_the_pdfs() -> None:
    rows = {r.losses: r for r in challenge.streaks(D(100), challenge.SCENARIOS[D(2)])}
    assert (rows[3].becomes, round(rows[3].to_recover * 100, 1)) == (D("72.90"), D("37.2"))
    assert (rows[7].becomes, round(rows[7].drawdown * 100, 1)) == (D("47.83"), D("-52.2"))
    assert rows[5].chance == D("0.03125")  # 3.12 % at 50 % losers
    assert rows[20].becomes == D("12.16")
    assert [r.losses for r in rows.values()] == list(challenge.STREAKS)


@pytest.mark.parametrize(
    ("rr", "typical", "fast", "slow", "within", "dd", "half"),
    [
        (D("1.5"), 182, 124, 264, 0.27, -0.54, 0.63),
        (D(2), 139, 94, 204, 0.27, -0.54, 0.60),
        (D(3), 89, 61, 132, 0.25, -0.52, 0.52),
    ],
)
def test_the_simulation_reproduces_the_pdfs_variance_table(
    rr: D, typical: int, fast: int, slow: int, within: float, dd: float, half: float
) -> None:
    s = challenge.SCENARIOS[rr]
    x = challenge.simulate_at(s.assumed_win, s)
    assert x.typical_trades is not None and abs(x.typical_trades - typical) <= 8
    assert x.fast_trades is not None and abs(x.fast_trades - fast) <= 8
    assert x.slow_trades is not None and abs(x.slow_trades - slow) <= 15
    assert abs(x.within_roadmap - within) <= 0.04
    assert abs(x.median_worst_drawdown - dd) <= 0.04
    assert abs(x.half_drawdown - half) <= 0.05
    assert x == challenge.simulate_at(s.assumed_win, s)  # a fixed seed: the same each time


def test_at_the_rules_tested_win_rate_the_plan_mostly_fails() -> None:
    """The honest gap the page shows: at 36.6 % winners (the BUY side's record at 1:2) and 10 %
    a trade, few runs reach $25,000 and many fall below $10 on the way."""
    x = challenge.simulate_at(D("0.366"), challenge.SCENARIOS[D(2)])
    assert x.within_roadmap < 0.02 and x.reached < 0.25
    assert x.unusable > 0.3 and x.median_worst_drawdown < -0.8


def _trade(pid: int, net: str, at: datetime) -> book.Trade:
    return book.Trade(pid, BUY, D("0.01"), at, at, at, D(4000), D(4001), D(net), 0)


def test_progress_counts_only_the_trades_since_the_plan_began() -> None:
    s = challenge.SCENARIOS[D(2)]
    trades = [
        _trade(1, "-7", START - timedelta(days=1)),  # before the plan: not counted
        _trade(2, "20", START + timedelta(hours=1)),
        _trade(3, "-12", START + timedelta(hours=2)),
        _trade(4, "-10.8", START + timedelta(hours=3)),
    ]
    p = challenge.progress(trades, started_at=START, start=D(100), balance=D("97.20"), s=s)
    assert (p.trades, p.wins, p.losses_in_a_row, p.longest_losing) == (3, 1, 2, 2)
    assert p.expected == D("115.76")  # the roadmap after 3 trades
    assert p.ahead == D("-18.56")
    assert p.peak == D(120) and p.drawdown == D("97.2") / D(120) - 1
    # No recorded start: worked out from the balance less the trades since (no deposits).
    q = challenge.progress(trades, started_at=START, start=None, balance=D("97.20"), s=s)
    assert q.start == D(100) and q.start_derived


def test_the_pdf_plan_as_rules() -> None:
    rules = risk.pdf_plan(D(2), START, D(100))
    assert (rules.risk_pct_default, rules.risk_pct_max) == (D(10), D(10))
    assert (rules.daily_trades, rules.weekdays_only, rules.cooling_off_hours) == (2, True, 0)
    assert (rules.daily_loss_pct, rules.max_open_risk_pct, rules.max_positions) == (D(20), D(20), 2)
    assert rules.plan == challenge.SCENARIOS[D(2)]
    with pytest.raises(ValueError, match=r"1\.5, 2 or 3"):
        risk.pdf_plan(D("2.5"), START, D(100))
    with pytest.raises(ValueError, match="both its reward and its start"):
        risk.RiskRules(plan_rr=D(2))
    assert risk.DEFAULT.plan is None


def test_the_pdfs_second_trade_after_a_loss_is_allowed_and_a_third_is_refused() -> None:
    """S117: the daily limit is a share of the day's starting balance. $100 → a 10 % loss →
    the next trade risks 10 % of $90 = $9: 10 + 9 = 19 ≤ 20 % of $100. A limit taken of the
    equity now (20 % of $90 = $18) would have refused it."""
    rules = risk.pdf_plan(D(2), START - timedelta(days=1), D(100))
    day = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
    lost = _account(
        balance=D(90),
        equity=D(90),
        margin_free=D(90),
        deals=(
            _deal(1, 1, BUY, ENTRY_IN, day + timedelta(hours=3)),
            _deal(2, 1, SELL, ENTRY_OUT, day + timedelta(hours=4), "-10"),
        ),
    )
    nine = Ticket(BUY, D("4000.35"), D("3991.35"), D("4018.35"), risk_pct=D(10))

    def check(acct: object) -> list:  # type: ignore[type-arg]
        sized = _size(nine, "90", max_risk_pct=D(10))
        return risk.check(acct, nine, sized, rules, NOW)  # type: ignore[arg-type]

    lines = check(lost)
    assert getattr(_size(nine, "90", max_risk_pct=D(10)), "risk_cash", None) == D("9.00")
    blocks = [x.text for x in lines if x.level == BLOCK]
    assert not [b for b in blocks if "daily" in b], blocks
    assert any("of a 20.00 daily loss limit" in x.text for x in lines)
    twice = _account(
        balance=D(90),
        equity=D(90),
        margin_free=D(90),
        deals=(
            _deal(1, 1, BUY, ENTRY_IN, day + timedelta(hours=3)),
            _deal(2, 1, SELL, ENTRY_OUT, day + timedelta(hours=4), "-10"),
            _deal(3, 2, BUY, ENTRY_IN, day + timedelta(hours=5)),
            _deal(4, 2, SELL, ENTRY_OUT, day + timedelta(hours=6), "0"),
        ),
    )
    third = [x.text for x in check(twice) if x.level == BLOCK]
    assert any("2 trades today: your plan allows 2 a day" in b for b in third), third


# The owner's real Exness account as terminal 3 reported it on 2026-11-25 (XAUUSDm): 1 lot =
# 100 oz, prices to 0.001 worth 0.1 a lot, 0.01 lot steps, no commission, swap in points (BUY
# -1297.2, SELL 0, three nights on Wednesday), Exness's own margin 176.11 a lot at 3522.129.
EXNESS_GOLD = {
    "contract_size": "100.0",
    "tick_size": "0.001",
    "tick_value": "0.1",
    "volume_min": "0.01",
    "volume_step": "0.01",
    "volume_max": "200.0",
    "digits": 3,
    "stops_level": 0,
    "point": "0.001",
    "swap_long": "-1297.2",
    "swap_short": "0.0",
    "swap_mode": 1,
    "swap_rollover3days": 3,
    "currency_profit": "USD",
    "currency_margin": "XAU",
    "freeze_level": 0,
    "calc_mode": 0,
    "margin_buy_1lot": "176.11",
    "margin_buy_at": "3522.129",
    "margin_sell_1lot": "176.1",
    "margin_sell_at": "3521.908",
    "profit_1lot_1": "100.0",
}


def test_the_plan_on_the_owners_exness_contract_at_their_balance() -> None:
    """The PDF plan's 10 % of 78.22 is 7.82: a 7.00 stop fits one 0.01 lot (risk 7.00, target
    +14.00 at 1:2); an 8.00 stop does not (0.01 lot would lose 8.00): NO TRADE, never more risk.
    A BUY held overnight pays 1.30 a night at 0.01 lot; a SELL nothing; margin about 1.76."""
    from datetime import timedelta

    from cosmic.manual.calc import size, swap_per_lot
    from cosmic.manual.live import SELL, Quote, spec_of

    spec = spec_of(EXNESS_GOLD, "XAUUSDm")
    assert spec is not None and spec.value_per_price == 100
    quote = Quote(D("3520.309"), D("3520.530"), NOW, timedelta(seconds=1))

    def sized(stop: str) -> object:
        return size(
            Ticket(BUY, D("3520.530"), D(stop), D("3534.530"), risk_pct=D(10)),
            equity=D("78.22"),
            margin_free=D("78.22"),
            leverage=2000,
            currency="USD",
            spec=spec,
            quote=quote,
            max_risk_pct=D(10),
            broker="Exness",
        )

    fits = sized("3513.530")  # 7.00 below the entry
    assert not fits.blocked and fits.lots == D("0.01")
    assert fits.risk_cash == D("7.00") and fits.reward_cash == D("14.00")
    assert fits.margin == (D("176.11") * D("3520.530") / D("3522.129") / 100)
    wide = sized("3512.530")  # 8.00 below: 0.01 lot loses 8.00, above 7.82
    assert wide.blocked
    assert challenge.next_trade(D("78.22"), challenge.SCENARIOS[D(2)]).risk == D("7.82")
    night = swap_per_lot(spec, BUY, D("3520"), "USD")
    assert night == D("-129.72") and night * D("0.01") == D("-1.2972")
    assert swap_per_lot(spec, SELL, D("3520"), "USD") == 0
