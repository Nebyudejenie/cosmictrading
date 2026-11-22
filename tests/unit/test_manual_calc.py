"""Manual Trading's calculator, risk rules and results (S113), on HFM's own gold contract: 1 lot
is 100 ounces, the price step 0.01 is worth 1.00 USD a lot, lots from 0.01 in steps of 0.01."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from cosmic.manual import book, risk
from cosmic.manual.calc import BLOCK, OK, WARN, Ticket, size
from cosmic.manual.insight import wilson
from cosmic.manual.live import (
    BUY,
    CONNECTED,
    ENTRY_IN,
    ENTRY_OUT,
    SELL,
    Deal,
    LiveAccount,
    Position,
    Quote,
    Spec,
)

GOLD = Spec(
    symbol="XAUUSD",
    contract_size=D(100),
    tick_size=D("0.01"),
    tick_value=D("1"),
    volume_min=D("0.01"),
    volume_step=D("0.01"),
    volume_max=D(50),
    digits=2,
    stops_level=0,
    point=D("0.01"),
    swap_long=None,
    swap_short=None,
    currency_profit="USD",
    currency_margin="XAU",
)
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)  # a Wednesday
QUOTE = Quote(D("4000.00"), D("4000.35"), NOW, timedelta(seconds=1))


def _size(t: Ticket, equity: str = "1000", **kw: object) -> object:
    args: dict[str, object] = {
        "equity": D(equity),
        "margin_free": D(equity),
        "leverage": 1000,
        "currency": "USD",
        "spec": GOLD,
        "quote": QUOTE,
        "max_risk_pct": D(2),
    }
    args.update(kw)
    return size(t, **args)  # type: ignore[arg-type]


def test_the_lot_size_is_rounded_down_to_hfms_step_and_never_risks_more_than_asked() -> None:
    # 1 % of 1,000 = 10.00; the stop 5.00 away loses 500.00 a lot: 0.02 lot, exactly 10.00.
    s = _size(Ticket(BUY, D("4000.35"), D("3995.35"), D("4010.35"), risk_pct=D(1)))
    assert (s.lots, s.risk_cash, s.reward_cash, s.rr) == (D("0.02"), D("10.00"), D("20.00"), 2)
    assert s.risk_pct == D(1) and not s.blocked
    assert s.margin == D("0.02") * 100 * D("4000.35") / 1000  # about 8.00 at 1:1000
    assert s.spread_cost == D("0.02") * D("0.35") * 100
    # 7.00 away: 700.00 a lot; 10.00 / 700 = 0.0142… is rounded DOWN to 0.01 (7.00 at risk).
    s = _size(Ticket(BUY, D("4000.35"), D("3993.35"), None, risk_pct=D(1)))
    assert (s.lots, s.risk_cash) == (D("0.01"), D("7.00"))
    assert any(x.level == WARN and "no target" in x.text for x in s.lines)


def test_a_sell_mirrors_a_buy_and_a_wrong_side_stop_is_refused() -> None:
    s = _size(Ticket(SELL, D("4000.00"), D("4005.00"), D("3990.00"), risk_amount=D(10)))
    assert (s.lots, s.risk_cash, s.reward_cash) == (D("0.02"), D("10.00"), D("20.00"))
    for t, why in (
        (Ticket(BUY, D(4000), D(4001), D(4010), risk_pct=D(1)), "below its entry"),
        (Ticket(SELL, D(4000), D(3999), D(3990), risk_pct=D(1)), "above its entry"),
        (Ticket(BUY, D(4000), D(3990), D(3995), risk_pct=D(1)), "target must be above"),
    ):
        r = _size(t)
        assert r.blocked and r.lots == 0 and any(why in x.text for x in r.lines)


def test_hfms_smallest_trade_too_big_for_the_risk_is_refused_with_its_numbers() -> None:
    """100 USD at 1 %: 1.00; the smallest trade (0.01 lot = 1 ounce) loses 10.00 at a 10.00
    stop: refused, never rounded up."""
    s = _size(Ticket(BUY, D("4000.35"), D("3990.35"), D("4020.35"), risk_pct=D(1)), "100")
    assert s.blocked and s.lots == 0
    (why,) = [x.text for x in s.lines if x.level == BLOCK]
    assert why.startswith("HFM's smallest trade (0.01 lot) would lose 10.00 at this stop-loss")
    assert "10.00 % of equity" in why


def test_the_risk_limit_the_margin_and_hfms_stop_distance_are_enforced() -> None:
    over = _size(Ticket(BUY, D("4000.35"), D("3995.35"), None, risk_pct=D("2.5")))
    assert over.blocked and "above your limit of 2 % a trade" in over.lines[-1].text
    poor = _size(Ticket(BUY, D("4000.35"), D("3995.35"), None, risk_pct=D(1)), margin_free=D("5"))
    assert poor.blocked and poor.lines[-1].text.startswith("not affordable")
    near = _size(
        Ticket(BUY, D("4000.35"), D("4000.00"), None, risk_pct=D(1)),
        spec=GOLD.__class__(**{**GOLD.__dict__, "stops_level": 50}),
    )
    assert near.blocked and "HFM allows no closer than 0.50" in near.lines[-1].text
    tight = _size(Ticket(BUY, D("4000.35"), D("3999.85"), D("4001.35"), risk_pct=D(1)))
    assert any("within two spreads" in x.text for x in tight.lines)
    far = _size(Ticket(BUY, D("4010.00"), D("4005.00"), D("4020.00"), risk_pct=D(1)))
    assert any("pending order" in x.text for x in far.lines)
    cap = _size(
        Ticket(BUY, D("4000.35"), D("3999.35"), None, risk_pct=D(2)),
        "10000000",
        margin_free=D("100000000"),
    )
    assert cap.lots == D(50) and any("HFM's largest trade" in x.text for x in cap.lines)


def test_a_cent_accounts_margin_is_counted_in_cents() -> None:
    s = _size(Ticket(BUY, D("4000.35"), D("3995.35"), None, risk_pct=D(1)), currency="USC")
    assert s.margin == D("0.02") * 100 * D("4000.35") / 1000 * 100


def _deal(t: int, pid: int, side: str, entry: int, at: datetime, net: str = "0") -> Deal:
    return Deal(
        t,
        pid,
        at,
        at + timedelta(hours=2),
        side,
        entry,
        D("0.01"),
        D(4000),
        D(net),
        D(0),
        D(0),
        D(0),
        0,
    )


def _account(**kw: object) -> LiveAccount:
    base: dict[str, object] = {
        "state": CONNECTED,
        "problem": None,
        "balance": D(1000),
        "equity": D(1000),
        "margin_free": D(1000),
        "spec": GOLD,
        "quote": QUOTE,
        "server_offset": timedelta(hours=2),
    }
    base.update(kw)
    return LiveAccount(**base)  # type: ignore[arg-type]


def _check(acct: LiveAccount, t: Ticket, rules: risk.RiskRules = risk.DEFAULT, **kw: object):
    s = _size(t, str(acct.equity))
    return risk.check(acct, t, s, rules, kw.get("now", NOW), kw.get("placed", ()))  # type: ignore[arg-type]


BUY_1 = Ticket(BUY, D("4000.35"), D("3995.35"), D("4010.35"), risk_pct=D(1))


def _blocks(lines: list) -> list[str]:  # type: ignore[type-arg]
    return [x.text for x in lines if x.level == BLOCK]


def test_a_trade_within_every_rule_passes() -> None:
    lines = _check(_account(), BUY_1)
    assert _blocks(lines) == [] and risk.verdict(lines) == OK


def test_the_daily_loss_limit_and_the_open_risk_refuse() -> None:
    day = NOW.replace(hour=0) - timedelta(hours=2) + timedelta(hours=2)
    lost = _account(
        deals=(
            _deal(1, 1, BUY, ENTRY_IN, day + timedelta(hours=3)),
            _deal(2, 1, SELL, ENTRY_OUT, day + timedelta(hours=4), "-25"),
        ),
    )
    assert any("past your daily limit of 30.00" in b for b in _blocks(_check(lost, BUY_1)))
    naked = _account(
        positions=(Position(7, BUY, D("0.01"), D(4000), None, None, None, D(0), D(0), None, 0, ""),)
    )
    assert any("#7 has no stop-loss" in b for b in _blocks(_check(naked, BUY_1)))
    heavy = _account(
        positions=(
            Position(8, SELL, D("0.10"), D(4100), D("4103.5"), None, None, D(0), D(0), None, 0, ""),
        )
    )
    # 3.50 × 0.10 lot × 100 = 35.00 open, + 10.00: above 4 % of 1,000.
    assert any("open risk would be 45.00" in b for b in _blocks(_check(heavy, BUY_1)))


def test_positions_losses_in_a_row_trades_a_day_and_duplicates() -> None:
    two = tuple(
        Position(n, SELL, D("0.01"), D(4100), D(4101), None, None, D(0), D(0), None, 0, "")
        for n in (1, 2)
    )
    assert any("2 positions are open" in b for b in _blocks(_check(_account(positions=two), BUY_1)))
    start = NOW - timedelta(hours=8)
    losers = tuple(
        d
        for k in range(3)
        for d in (
            _deal(10 + 2 * k, 100 + k, BUY, ENTRY_IN, start + timedelta(minutes=30 * k)),
            _deal(
                11 + 2 * k, 100 + k, SELL, ENTRY_OUT, start + timedelta(minutes=30 * k + 10), "-1"
            ),
        )
    )
    blocks = _blocks(_check(_account(deals=losers), BUY_1))
    assert any(b.startswith("3 losing trades in a row: cooling off until") for b in blocks)
    assert any("3 trades today: your plan allows 2 a day" in b for b in blocks)
    later = _blocks(_check(_account(deals=losers), BUY_1, now=NOW + timedelta(days=1)))
    assert not any("cooling off" in b for b in later)  # 24 hours after the last loss
    placed = (risk.Recent(BUY, NOW - timedelta(seconds=40)),)
    assert any(
        "looks like a duplicate" in b for b in _blocks(_check(_account(), BUY_1, placed=placed))
    )
    same = _account(
        positions=(
            Position(9, BUY, D("0.01"), D(3990), D(3985), None, None, D(0), D(0), None, 0, ""),
        )
    )
    lines = _check(same, BUY_1)
    assert any(x.level == WARN and "adds to your open BUY" in x.text for x in lines)


def test_the_weekend_a_stale_price_and_an_account_not_live_refuse() -> None:
    saturday = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    assert any("Monday to Friday" in b for b in _blocks(_check(_account(), BUY_1, now=saturday)))
    stale = _account(quote=Quote(D(4000), D("4000.35"), NOW, timedelta(minutes=3)))
    assert any("no current price" in b for b in _blocks(_check(stale, BUY_1)))
    off = _account(state="NOT_LIVE", problem="HFM reports a DEMO account")
    assert any(
        b.startswith("no live account: HFM reports a DEMO") for b in _blocks(_check(off, BUY_1))
    )


def test_more_risk_right_after_a_loss_is_warned_and_the_rules_have_hard_ceilings() -> None:
    lost = _account(
        deals=(
            _deal(1, 1, BUY, ENTRY_IN, NOW - timedelta(hours=3)),
            _deal(2, 1, SELL, ENTRY_OUT, NOW - timedelta(hours=2), "-5"),
        ),
    )
    raised = Ticket(BUY, D("4000.35"), D("3995.35"), D("4010.35"), risk_pct=D("1.5"))
    assert any("never raise the size" in x.text for x in _check(lost, raised))
    with pytest.raises(ValueError):
        risk.RiskRules(risk_pct_max=D(11))
    with pytest.raises(ValueError):
        risk.RiskRules(risk_pct_default=D(3), risk_pct_max=D(2))
    assert (risk.DEFAULT.risk_pct_default, risk.DEFAULT.risk_pct_max) == (1, 2)


def test_results_from_hfms_deals() -> None:
    t0 = NOW - timedelta(days=2)
    deals = []
    for k, net in enumerate(("20", "-10", "30", "-10")):
        deals += [
            _deal(2 * k, k + 1, BUY, ENTRY_IN, t0 + timedelta(hours=k)),
            _deal(2 * k + 1, k + 1, SELL, ENTRY_OUT, t0 + timedelta(hours=k, minutes=30), net),
        ]
    trades = book.closed_trades(deals)
    s = book.stats(trades)
    assert (s.trades, s.wins, s.win_rate) == (4, 2, 50)
    assert (s.avg_win, s.avg_loss, s.profit_factor, s.expectancy) == (25, -10, D("2.5"), 7.5)
    assert s.max_drawdown == 10 and s.net == 30
    assert book.losses_in_a_row(trades) == 1


def test_the_95_percent_range_of_a_record() -> None:
    low, high = wilson(D("0.347"), 124)
    assert (low, high) == (D("0.2690"), D("0.4342"))  # 26.9 % to 43.4 %
    assert wilson(D("0.5"), 0) == (D(0), D(1))
