"""The internal gold simulation (S62): the owner's balance, amount and risk, HFM's prices,
fractional ounces, BUY and SELL, stop-loss and take-profit, P/L, limits, once per candle.
SIMULATED: nothing is sent to HFM.

Gold here: HFM at 4,040.00 / 4,040.30; hourly candles rising 1 an hour with a range of 20, so
ATR(14) = 20 and the stop is 1.5 × 20 = 30.00 from the entry."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings as AppSettings
from cosmic.core.errors import ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_gold import gold_sim_accounts, gold_sim_decisions, gold_sim_ledger
from cosmic.db.schema_market import instruments, sources
from cosmic.db.schema_trading import bridge_commands, bridge_state
from cosmic.gold import analysis, sim
from cosmic.market import hfm_candles
from cosmic.sources.ingest import ensure_registry
from cosmic.web.app import create_app
from tests.helpers import TestUser as OwnerUser
from tests.helpers import client_for, csrf_from, full_login, make_user

WED = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)  # 10:00 in New York: gold trades
HFM_GOLD = {  # HFM's own rules for XAUUSD, as its report gave them on 2026-11-04
    "swap_mode": 1,
    "swap_long": "-72.04",
    "swap_short": "0.0",
    "swap_rollover3days": 3,
    "contract_size": "100.0",
    "point": "0.01",
}


class World:
    def __init__(self, db: Database, clock: FrozenClock, owner: uuid.UUID) -> None:
        self.db, self.clock, self.owner = db, clock, owner

    async def hfm(self, bid: str, ask: str, *, tick_age: timedelta = timedelta(0)) -> None:
        """HFM's report as the bridge stores it every second."""
        now = self.clock.now()
        snap = {
            "account": {"trade_mode": "DEMO", "currency": "USD", "leverage": 1000},
            "ticks": {
                "XAUUSD": {
                    "bid": bid,
                    "ask": ask,
                    "time_msc": int((now - tick_age).timestamp() * 1000) + 7_200_000,
                }
            },
            "server_utc_offset_ms": 7_200_000,
            "symbols": {"XAUUSD": HFM_GOLD},
        }
        async with self.db.transaction() as conn:
            await conn.execute(text("DELETE FROM bridge_state"))
            await conn.execute(
                bridge_state.insert().values(bridge="hfm-demo", received_at=now, snapshot=snap)
            )

    async def candles(self, tf: str, closes: Sequence[float], half: float) -> None:
        """Complete candles of HFM gold, the last one just closed."""
        now = self.clock.now()
        last = hfm_candles.open_of(now, tf) - timedelta(seconds=hfm_candles.TIMEFRAMES[tf])
        step = timedelta(seconds=hfm_candles.TIMEFRAMES[tf])
        async with self.db.transaction() as conn:
            inst = await conn.scalar(
                select(instruments.c.id).where(instruments.c.canonical_key == "CFD:XAU/USD@HFM")
            )
            src = await conn.scalar(select(sources.c.id).where(sources.c.key == "hfm_mt5_bridge"))
            await conn.execute(
                text("DELETE FROM bars WHERE instrument_id = :i AND timeframe = :tf"),
                {"i": inst, "tf": tf},
            )
            run = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO fetch_runs (id, source_id, kind, started_at, finished_at, status) "
                    "VALUES (:r, :s, 't', :n, :n, 'SUCCESS')"
                ),
                {"r": run, "s": src, "n": now},
            )
            for k, c in enumerate(closes):
                t = last - step * (len(closes) - 1 - k)
                await conn.execute(
                    text(
                        "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, "
                        "high, low, close, volume, is_complete, fetch_run_id, ingested_at, "
                        "updated_at) VALUES (:i, :s, :tf, :t, :c, :h, :l, :c, 1, true, :r, :n, :n)"
                    ),
                    {
                        "i": inst,
                        "s": src,
                        "tf": tf,
                        "t": t,
                        "c": D(str(round(c, 4))),
                        "h": D(str(round(c + half, 4))),
                        "l": D(str(round(c - half, 4))),
                        "r": run,
                        "n": now,
                    },
                )

    async def market(self, side: str = "BUY") -> None:
        """Hourly trend and a 1-minute turn: BUY (rising) or SELL (the mirror)."""
        sign = 1 if side == "BUY" else -1
        await self.candles("1h", [4000 + sign * i for i in range(40)], half=10)
        up = [4030 + 0.2 * i for i in range(30)]
        down = [up[-1] - 0.9 * (i + 1) for i in range(8)]
        m1 = [*up, *down, down[-1] + 6.0]
        await self.candles("1m", m1 if side == "BUY" else [2 * m1[0] - x for x in m1], 0.3)
        await self.candles("1d", [4000.0 + i for i in range(5)], 20)  # too few: no daily filter

    async def account(self, balance: str, amount: str | None = None, **kw: object) -> uuid.UUID:
        s = sim.Settings(
            amount=D(amount or balance),
            risk_pct=D(2),
            daily_loss=D(balance) / 10,
            **kw,  # type: ignore[arg-type]
        )
        async with self.db.transaction() as conn:
            return await sim.create(
                conn,
                name=f"gold {balance} {uuid7()}"[:40],
                starting_balance=D(balance),
                settings=s,
                owner=self.owner,
                now=self.clock.now(),
            )

    async def positions(self, account_id: uuid.UUID) -> list[sim.Position]:
        async with self.db.connect() as conn:
            return await sim.positions(conn, account_id, status=None)

    async def state(self, account_id: uuid.UUID) -> sim.State:
        async with self.db.connect() as conn:
            price = await sim.price_now(conn, self.clock.now())
            return await sim.state(conn, account_id, price, self.clock.now())

    async def decisions(self, account_id: uuid.UUID) -> list[object]:
        async with self.db.connect() as conn:
            return list(
                await conn.execute(
                    select(gold_sim_decisions)
                    .where(gold_sim_decisions.c.account_id == account_id)
                    .order_by(gold_sim_decisions.c.bar)
                )
            )


@pytest.fixture
async def w(settings: AppSettings, db: Database) -> World:
    clock = FrozenClock(WED)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
    owner = await make_user(db, clock)
    world = World(db, clock, owner.user_id)
    await world.hfm("4040.00", "4040.30")
    return world


# ---- the size, the settings and the reading, pure -----------------------------------------------


def _state(money: str) -> sim.State:
    m = D(money)
    return sim.State(m, D(0), m, D(0), m, m, D(0), [])


def test_the_size_follows_your_amount_and_never_your_risk() -> None:
    sizes = {}
    for amount in ("10", "20", "50", "100", "200", "500"):
        s = sim.Settings(amount=D(amount), risk_pct=D(2), daily_loss=D(5))
        sizes[amount] = sim.size(s, _state(amount), D(30), D("4040.30"), D(1000))
    assert {a: z.ounces for a, z in sizes.items()} == {
        "10": D(0),
        "20": D("0.01"),
        "50": D("0.03"),
        "100": D("0.06"),
        "200": D("0.13"),
        "500": D("0.33"),
    }
    too_small = sizes["10"]
    assert too_small.why_not == (
        "Your selected trading amount is too small for this opportunity at your chosen risk "
        "level: the smallest trade (0.01 ounce) would lose about 0.30 at its stop, more than the "
        "0.20 you allow (2 % of 10.00). At 2 %, this opportunity trades from an amount of 15.00"
    )
    assert too_small.smallest_amount == D("15.00")
    assert "risk of" not in too_small.why_not  # the risk is never suggested to change
    assert sizes["100"].margin == D("0.25")  # 0.06 oz x 4,040.30 at HFM's 1:1000
    # The money left, not the amount, when the account has lost: never more than it has.
    lost = sim.size(
        sim.Settings(amount=D(100), risk_pct=D(2), daily_loss=D(5)),
        _state("50"),
        D(30),
        D("4040.30"),
        D(1000),
    )
    assert lost.ounces == D("0.03") and lost.max_loss == D("1.0000")


def test_each_setting_is_checked_on_its_own_and_never_changed() -> None:
    ok = sim.Settings(amount=D(100), risk_pct=D(2), daily_loss=D("0.10"))
    assert sim.problems(ok, D(100)) == []  # a daily limit below one trade's risk is the owner's
    wrong = sim.Settings(
        amount=D(150),
        risk_pct=D(12),
        daily_loss=D(0),
        max_open_risk_pct=D(0),
        max_drawdown_pct=D(0),
        max_positions=11,
        take_profit_r=D("0.1"),
    )
    assert sim.problems(wrong, D(100)) == [
        "Trading amount: 150.00 is more than the account's balance (100.00); the system trades "
        "only money the account has.",
        "Risk per trade: 12 % is outside 0.1 % to 10 %.",
        "Most a day may lose: it must be more than 0.",
        "Most at risk at once: 0 % of the amount is outside 0.1 % to 100 %.",
        "Largest drop: 0 % is outside 1 % to 100 %.",
        "Trades open at once: 11 is outside 1 to 10.",
        "Take-profit: 0.1 × the stop distance is outside 0.5 to 10 (or choose none: the rule "
        "closes it).",
    ]


async def test_the_reading_says_buy_sell_or_wait_with_reasons(w: World) -> None:
    await w.market("BUY")
    async with w.db.connect() as conn:
        buy = await sim.reading_now(conn, w.clock.now())
    assert buy.decision == analysis.BUY and buy.stop_distance == D("30.00")
    assert buy.reasons == [
        "H1 trend UP",
        "D1: 5 of 21 days stored, not used yet",
        "M1 momentum turned up above its average",
    ]
    assert buy.frames["1h"].atr == 20.0 and buy.frames["1m"].trend in (analysis.UP, "SIDEWAYS")
    await w.market("SELL")
    async with w.db.connect() as conn:
        sell = await sim.reading_now(conn, w.clock.now())
    assert sell.decision == analysis.SELL and sell.reasons[-1].startswith("M1 momentum turned")
    w.clock.advance(minutes=10)  # no new 1-minute candle for ten minutes: closed or stalled
    async with w.db.connect() as conn:
        stale = await sim.reading_now(conn, w.clock.now())
    assert stale.decision == analysis.WAIT and "no new candle lately" in stale.reasons[-1]


# ---- the simulation on HFM's prices --------------------------------------------------------------


async def test_a_simulated_buy_opens_at_hfms_ask_and_its_target_closes_it(w: World) -> None:
    acct = await w.account("100")
    await w.market("BUY")
    assert await sim.decide(w.db, w.clock.now()) == [f"{acct}:BUY"]
    (p,) = await w.positions(acct)
    assert (p.side, p.ounces, p.entry_price) == ("BUY", D("0.06"), D("4040.30"))
    assert (p.stop_price, p.take_profit_price, p.margin) == (D("4010.30"), D("4100.30"), D("0.25"))
    assert p.spread == D("0.30")
    st = await w.state(acct)
    # Floating P/L at HFM's bid: (4,040.00 - 4,040.30) x 0.06 = -0.018; equity follows.
    assert (st.balance, st.floating, st.equity) == (D(100), D("-0.0180"), D("99.9820"))
    w.clock.advance(seconds=30)
    await w.hfm("4100.50", "4100.80")  # gold rises past the target
    assert await sim.watch(w.db, w.clock.now()) == ["BUY:TAKE_PROFIT"]
    (p,) = await w.positions(acct)
    assert (p.status, p.exit_reason, p.exit_price) == ("CLOSED", "TAKE_PROFIT", D("4100.50"))
    assert p.result == D("3.6120")  # (4,100.50 - 4,040.30) x 0.06
    st = await w.state(acct)
    assert st.balance == D("103.6120") and st.equity == st.balance and not st.positions
    async with w.db.connect() as conn:  # nothing went to HFM
        assert await conn.scalar(select(func.count()).select_from(bridge_commands)) == 0


async def test_a_simulated_sell_stopped_through_a_jump_loses_more_than_planned(w: World) -> None:
    acct = await w.account("100")
    await w.market("SELL")
    assert await sim.decide(w.db, w.clock.now()) == [f"{acct}:SELL"]
    (p,) = await w.positions(acct)
    assert (p.side, p.ounces, p.entry_price, p.stop_price) == (
        "SELL",
        D("0.06"),
        D("4040.00"),  # a sell opens at HFM's bid
        D("4070.00"),
    )
    assert p.take_profit_price == D("3980.00")
    w.clock.advance(seconds=5)
    await w.hfm("4074.70", "4075.00")  # gold jumps past the stop
    assert await sim.watch(w.db, w.clock.now()) == ["SELL:STOP_LOSS"]
    (p,) = await w.positions(acct)
    assert (p.exit_price, p.result) == (D("4075.00"), D("-2.1000"))  # planned 1.80
    assert (await w.state(acct)).balance == D("97.9000")


async def test_too_small_says_wait_and_from_which_amount(w: World) -> None:
    acct = await w.account("10")
    await w.market("BUY")
    assert await sim.decide(w.db, w.clock.now()) == [f"{acct}:WAIT"]
    (d,) = await w.decisions(acct)
    assert (d.decision, d.action) == ("BUY", "WAIT")  # the opportunity, and what was done
    assert d.reasons[-1].startswith(
        "WAIT — Your selected trading amount is too small for this opportunity at your chosen "
        "risk level"
    )
    assert d.reasons[-1].endswith("this opportunity trades from an amount of 15.00")
    assert await w.positions(acct) == []


async def test_a_candle_is_decided_once_even_after_a_restart(w: World) -> None:
    acct = await w.account("100")
    await w.market("BUY")
    assert await sim.decide(w.db, w.clock.now()) == [f"{acct}:BUY"]
    assert await sim.decide(w.db, w.clock.now()) == []  # the same candle: nothing more
    w.clock.advance(seconds=20)  # a restarted worker, the same candle still the latest
    assert await sim.decide(w.db, w.clock.now()) == []
    assert len(await w.decisions(acct)) == 1 and len(await w.positions(acct)) == 1


async def test_each_limit_says_why_it_waits(w: World) -> None:
    acct = await w.account("100", max_positions=1)
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    w.clock.advance(minutes=1)
    await w.hfm("4040.00", "4040.30")  # the bridge reports every second
    await w.market("BUY")
    assert await sim.decide(w.db, w.clock.now()) == [f"{acct}:WAIT"]
    assert (await w.decisions(acct))[-1].reasons[-1] == "WAIT — 1 trade(s) open: your limit is 1"
    no_sell = await w.account("100", allow_sell=False)  # the new one; the first stops
    w.clock.advance(minutes=1)
    await w.hfm("4040.00", "4040.30")  # the bridge reports every second
    await w.market("SELL")
    assert await sim.decide(w.db, w.clock.now()) == [f"{no_sell}:WAIT"]
    assert (await w.decisions(no_sell))[-1].reasons[-1] == "WAIT — selling is off in your settings"
    lost = await w.account("100")
    async with w.db.transaction() as conn:  # a day that already lost 10.00
        await conn.execute(
            gold_sim_ledger.insert().values(
                id=uuid7(),
                account_id=lost,
                at=w.clock.now(),
                kind="RESULT",
                amount=D(-10),
                ref=f"test:{lost}",
                note="a losing day",
            )
        )
    w.clock.advance(minutes=1)
    await w.hfm("4040.00", "4040.30")  # the bridge reports every second
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    assert (await w.decisions(lost))[-1].reasons[-1] == (
        "WAIT — today's loss (10.00) reached your daily limit of 10.00"
    )


async def test_the_rule_closes_a_trade_the_hourly_trend_turns_against(w: World) -> None:
    acct = await w.account("100")
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    w.clock.advance(minutes=1)
    await w.hfm("4031.00", "4031.30")
    await w.market("SELL")  # the hourly trend has turned down
    await sim.decide(w.db, w.clock.now())
    first = (await w.positions(acct))[-1]
    assert (first.side, first.exit_reason, first.exit_price) == ("BUY", "RULE", D("4031.00"))
    assert first.result == D("-0.5580")  # (4,031.00 - 4,040.30) x 0.06


async def test_swap_is_hfms_once_a_night_and_three_on_wednesday(w: World) -> None:
    w.clock.set(WED - timedelta(days=1))  # Tuesday 14:00 UTC
    await w.hfm("4040.00", "4040.30")
    acct = await w.account("100")
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    w.clock.set(WED + timedelta(days=1))  # Thursday: Tuesday's night, and Wednesday's x 3
    await w.hfm("4041.00", "4041.30")
    await sim.watch(w.db, w.clock.now())
    await sim.watch(w.db, w.clock.now())  # once a night, however often it looks
    async with w.db.connect() as conn:
        swaps = [
            r.amount
            for r in await conn.execute(
                select(gold_sim_ledger.c.amount).where(gold_sim_ledger.c.kind == "SWAP")
            )
        ]
    assert swaps == [D("-0.0432")] * 4  # 72.04 points = 0.7204 an ounce a night, x 0.06 oz
    st = await w.state(acct)
    assert st.balance == D(100) + 4 * D("-0.0432")
    (p,) = await w.positions(acct)
    assert p.swap == 4 * D("-0.0432")


async def test_no_current_price_no_action(w: World) -> None:
    acct = await w.account("100")
    await w.market("BUY")
    await w.hfm("4040.00", "4040.30", tick_age=timedelta(minutes=6))  # no tick for six minutes
    await sim.decide(w.db, w.clock.now())
    assert await w.positions(acct) == []
    assert (await w.decisions(acct))[-1].reasons[-1] == "no current HFM price"


async def test_stopping_closes_every_trade_at_hfms_price(w: World) -> None:
    acct = await w.account("100")
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    await w.hfm("4045.00", "4045.30")
    async with w.db.transaction() as conn:
        assert await sim.close_all(conn, acct, w.clock.now(), "the owner stopped it") == 1
        await sim.set_running(conn, acct, False, "stopped by the owner", w.clock.now())
    (p,) = await w.positions(acct)
    assert (p.exit_reason, p.result) == ("OWNER", D("0.2820"))  # (4,045.00 - 4,040.30) x 0.06
    w.clock.advance(minutes=1)
    await w.market("BUY")
    assert await sim.decide(w.db, w.clock.now()) == []  # a stopped account does nothing


async def test_settings_are_refused_with_the_reason_and_kept(w: World) -> None:
    with pytest.raises(ValidationFailedError, match=r"Trading amount: 200\.00 is more than"):
        async with w.db.transaction() as conn:
            await sim.create(
                conn,
                name="too much",
                starting_balance=D(100),
                settings=sim.Settings(amount=D(200), risk_pct=D(2), daily_loss=D(5)),
                owner=w.owner,
                now=w.clock.now(),
            )


async def test_the_pages_start_a_simulation_and_show_it(
    w: World, settings: AppSettings, db: Database
) -> None:
    await w.market("BUY")
    app = create_app(settings, db=db, clock=w.clock)
    async with client_for(app) as c:
        user = await _owner_user(w)
        await full_login(c, user, w.clock)
        await w.hfm("4040.00", "4040.30")
        page = (await c.get("/gold/sim")).text
        assert "Start your gold simulation" in page and "Nothing here is sent to HFM" in page
        r = await c.post(
            "/gold/sim/start",
            data={
                "csrf_token": csrf_from(page),
                "name": "My gold",
                "starting_balance": "100",
                "amount": "50",
                "risk_pct": "2",
                "daily_loss": "3",
                "max_positions": "1",
                "take_profit_r": "2",
                "allow_sell": "on",
            },
        )
        assert r.status_code == 303
        await sim.decide(w.db, w.clock.now())
        page = (await c.get("/gold/sim")).text
        assert "My gold" in page and "RUNNING" in page and "SIMULATED" in page
        assert "BUY" in page and "0.03" in page  # $50 at 2 %: 1.00 / 30.00 = 0.03 ounce
        analysis_page = (await c.get("/gold/analysis")).text
        assert "Gold analysis" in analysis_page and "H1" in analysis_page and "BUY" in analysis_page
        bad = await c.post(
            "/gold/sim/settings",
            data={
                "csrf_token": csrf_from(page),
                "account": str((await _accounts(w))[0]),
                "amount": "500",
                "risk_pct": "2",
                "daily_loss": "3",
            },
        )
        assert bad.status_code == 422 and "is more than the account" in bad.text


async def _owner_user(w: World) -> OwnerUser:
    return await make_user(w.db, w.clock, username="gold-owner")


async def _accounts(w: World) -> list[uuid.UUID]:
    async with w.db.connect() as conn:
        return [r.id for r in await conn.execute(select(gold_sim_accounts.c.id))]


@pytest.mark.keeps_binance_paused
def test_binance_is_paused_as_the_product_ships() -> None:
    from cosmic.trading import mandates

    assert frozenset({"BINANCE", "BINANCE_TESTNET"}) == mandates.PAUSED_VENUES
    assert mandates.paused("SPOT:PAXG/USDT@BINANCE") is not None
    # The old HFM gold auto-trader read Binance's PAXG for its signal: paused too.
    assert "BINANCE" in (mandates.paused("CFD:XAU/USD@HFM") or "")
    assert mandates.paused("SPOT:BTC/USD@COINBASE") is None


async def test_a_stop_without_a_price_closes_at_the_next_one(w: World) -> None:
    """Review, S62: stopping (or a new simulation) when HFM has no current price left the trades
    open and unwatched. They now keep their stop and target, and close at HFM's next price."""
    acct = await w.account("100")
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    await w.hfm("4040.00", "4040.30", tick_age=timedelta(minutes=6))  # no current price
    async with w.db.transaction() as conn:
        assert await sim.close_all(conn, acct, w.clock.now(), "stopped") == 0
        await sim.set_running(conn, acct, False, "stopped by the owner", w.clock.now())
    (p,) = await w.positions(acct)
    assert p.status == "OPEN"
    w.clock.advance(minutes=1)
    await w.hfm("4044.00", "4044.30")  # the next price
    assert await sim.watch(w.db, w.clock.now()) == ["BUY:OWNER"]
    (p,) = await w.positions(acct)
    assert (p.exit_reason, p.exit_price) == ("OWNER", D("4044.00"))


async def test_a_new_simulation_closes_the_old_ones_trades(w: World) -> None:
    first = await w.account("100")
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    await w.hfm("4041.00", "4041.30")
    second = await w.account("50")
    (p,) = await w.positions(first)
    assert (p.status, p.exit_reason, p.exit_price) == ("CLOSED", "OWNER", D("4041.00"))
    async with w.db.connect() as conn:
        assert await sim.running_accounts(conn) == [second]


async def test_the_nights_held_are_paid_even_when_the_stop_closes_it(w: World) -> None:
    """Review, S62: silent across a rollover, then the first price crosses the stop: the swap of
    the night is booked, then the result."""
    w.clock.set(WED - timedelta(days=1))  # Tuesday 14:00 UTC
    await w.hfm("4040.00", "4040.30")
    acct = await w.account("100")
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    w.clock.set(WED - timedelta(hours=10))  # after Tuesday's rollover (17:00 New York)
    await w.hfm("4000.00", "4000.30")  # past the stop at 4,010.30
    assert await sim.watch(w.db, w.clock.now()) == ["BUY:STOP_LOSS"]
    async with w.db.connect() as conn:
        kinds = [
            r.kind
            for r in await conn.execute(
                select(gold_sim_ledger.c.kind)
                .where(gold_sim_ledger.c.account_id == acct)
                .order_by(gold_sim_ledger.c.at, gold_sim_ledger.c.id)
            )
        ]
    assert kinds == ["DEPOSIT", "SWAP", "RESULT"]


def test_a_limit_at_once_below_one_trade_is_refused_with_why() -> None:
    s = sim.Settings(amount=D(20), risk_pct=D(2), daily_loss=D(1), max_open_risk_pct=D(1))
    assert sim.problems(s, D(20)) == [
        "Most at risk at once: 1 % is less than one trade's risk (2 %), so no trade could ever "
        "open; make it at least that."
    ]


def test_hfms_swap_is_counted_in_decimal_from_its_own_terms() -> None:
    assert sim.swap_per_ounce(HFM_GOLD, "BUY", D("4040.30")) == D("-0.7204")
    assert sim.swap_per_ounce(HFM_GOLD, "SELL", D("4040.30")) == D("0.000")
