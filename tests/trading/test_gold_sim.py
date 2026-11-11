"""The internal gold simulation (S62): the owner's balance, amount and risk, HFM's prices,
fractional ounces, BUY and SELL, stop-loss and take-profit, P/L, limits, once per candle.
SIMULATED: nothing is sent to HFM.

Gold here: HFM at 4,040.00 / 4,040.30; hourly candles rising 1 an hour with a range of 20, so
ATR(14) = 20 and the stop is 1.5 × 20 = 30.00 from the entry."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings as AppSettings
from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_gold import gold_sim_accounts, gold_sim_decisions, gold_sim_ledger
from cosmic.db.schema_market import instruments, sources
from cosmic.db.schema_trading import bridge_commands, bridge_state
from cosmic.gold import analysis, board, sim
from cosmic.market import hfm_candles
from cosmic.sources.ingest import ensure_registry
from cosmic.trading.policy import ensure_default_policy
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
        await ensure_default_policy(conn, clock.now())  # as production always has one
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
        "level: the smallest trade (0.01 ounce) would lose about 0.30 at its stop (costs "
        "included), more than the 0.20 you allow (2 % of 10.00). At 2 %, this opportunity trades "
        "from an amount of 15.00"
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
    # S77: an ounce's costs (two spreads of 0.30 as slippage) count in its loss at the stop.
    costed = sim.size(
        sim.Settings(amount=D(100), risk_pct=D(2), daily_loss=D(6)),
        _state("100"),
        D(15),
        D("4040.30"),
        D(1000),
        sim.costs(D("0.30")),
    )
    assert (costed.per_ounce, costed.ounces) == (D("15.60"), D("0.12"))  # 2.00 / 15.60
    assert costed.ounces * costed.per_ounce == D("1.8720")  # never above the 2.00


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
    # The target is twice the whole risk of an ounce, costs included: 30.00 + 2 × 0.30 (S77).
    assert (p.stop_price, p.take_profit_price, p.margin) == (D("4010.30"), D("4101.50"), D("0.25"))
    assert p.spread == D("0.30")
    st = await w.state(acct)
    # Floating P/L at HFM's bid: (4,040.00 - 4,040.30) x 0.06 = -0.018; equity follows.
    assert (st.balance, st.floating, st.equity) == (D(100), D("-0.0180"), D("99.9820"))
    w.clock.advance(seconds=30)
    await w.hfm("4101.70", "4102.00")  # gold rises past the target
    assert await sim.watch(w.db, w.clock.now()) == ["BUY:TAKE_PROFIT"]
    (p,) = await w.positions(acct)
    assert (p.status, p.exit_reason, p.exit_price) == ("CLOSED", "TAKE_PROFIT", D("4101.70"))
    assert p.result == D("3.6840")  # (4,101.70 - 4,040.30) x 0.06
    st = await w.state(acct)
    assert st.balance == D("103.6840") and st.equity == st.balance and not st.positions
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
    assert p.take_profit_price == D("3978.80")  # 2 × (30.00 + 0.60 of costs) below
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
    assert d.reasons[-1].endswith("this opportunity trades from an amount of 15.30")  # 30.60
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


async def test_the_panel_reviews_starts_and_shows_it(
    w: World, settings: AppSettings, db: Database
) -> None:
    """The owner's whole workflow on My Gold Auto-Trader: an amount, Simulation, automation ON,
    Automatic; the review shows what the system calculated; START with the code; RUNNING or
    WAITING, honestly; settings changed in place (it never switches off); automation OFF."""
    await w.market("BUY")
    app = create_app(settings, db=db, clock=w.clock)
    async with client_for(app) as c:
        user = await _owner_user(w)
        await full_login(c, user, w.clock)
        await w.hfm("4040.00", "4040.30")
        page = (await c.get("/gold/sim")).text
        assert "My Gold Auto-Trader" in page and "nothing is sent to HFM" in page
        assert "READY" in page and "<strong>HFM Demo</strong>real orders" in page
        assert "4. START" in page and "Review, then START" not in page  # one step (S65)
        # HFM Demo with $50: HFM's smallest trade (1 ounce) would lose 34.04 at the stop, more
        # than 1.00; nothing starts, and the simulation with the same $50 is offered instead.
        form = {"csrf_token": csrf_from(page), "amount": "50", "permission": "AUTO"}
        hfm = await c.post("/gold/sim/start", data={**form, "mode": "HFM_DEMO"})
        assert hfm.status_code == 200, re.findall(r'role="alert">([^<]*)', hfm.text)
        assert "Nothing was started on HFM" in hfm.text
        assert "demo trades this from an amount of" in hfm.text
        assert "START the simulation with 50.00 USD" in hfm.text
        async with db.connect() as conn:
            assert await sim.active_accounts(conn) == []  # nothing started
        hidden = dict(
            re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)"', hfm.text)
        )
        assert hidden["mode"] == "SIMULATION" and hidden["amount"] == "50"
        r = await c.post("/gold/sim/start", data=hidden)
        assert r.status_code == 303 and r.headers["location"] == "/gold/sim?started=1"
        # $50 at the calculated 2 %: 1.00 a trade, 3.00 a day (three trades), 2.50 at risk at
        # once; a stop 30.00 away: 0.03 ounce.
        page = (await c.get("/gold/sim")).text
        for shown in ("1.00", "3.00", "2.50", "30.00"):
            assert shown in page, shown
        page = (await c.get("/gold/sim?started=1")).text
        assert "✓ Started" in page and "WAITING" in page  # no decision yet: it waits
        await sim.decide(w.db, w.clock.now())
        page = (await c.get("/gold/sim")).text
        assert "RUNNING" in page and "1 trade open" in page and "0.03" in page
        (acct,) = await _accounts(w)
        assert (await _setting(w, acct)).daily_loss == D("3.00")
        changed = await c.post(
            "/gold/sim/settings",
            data={
                "csrf_token": csrf_from(page),
                "account": str(acct),
                "amount": "40",
                "permission": "ASK",
                "risk_pct": "2",
                "daily_loss": "3",
            },
        )
        assert changed.status_code == 303
        assert (await _status(w, acct)) == "RUNNING"  # saved in place: never off and on
        assert (await _setting(w, acct)).permission == "ASK"
        bad = await c.post(
            "/gold/sim/settings",
            data={"csrf_token": csrf_from(page), "account": str(acct), "amount": "500"},
        )
        assert bad.status_code == 422 and "is more than the account" in bad.text
        off = await c.post(
            "/gold/sim/automation",
            data={"csrf_token": csrf_from(page), "account": str(acct), "automation": "OFF"},
        )
        assert off.status_code == 303 and (await _status(w, acct)) == "PAUSED"
        page = (await c.get("/gold/sim")).text
        assert "PAUSED" in page and "open trades keep their stop-loss" in page
        analysis_page = (await c.get("/gold/analysis")).text
        assert "Gold analysis" in analysis_page and "H1" in analysis_page


async def _status(w: World, account: uuid.UUID) -> str:
    async with w.db.connect() as conn:
        return str(
            await conn.scalar(
                select(gold_sim_accounts.c.status).where(gold_sim_accounts.c.id == account)
            )
        )


async def _setting(w: World, account: uuid.UUID) -> sim.Settings:
    async with w.db.connect() as conn:
        s = await sim.settings_of(conn, account)
    assert s is not None
    return s


async def _told(w: World) -> list[tuple[str, str]]:
    async with w.db.connect() as conn:
        rows = await conn.execute(
            select(notifications.c.payload, notifications.c.channel)
            .where(notifications.c.kind == "gold_sim")
            .order_by(notifications.c.created_at)
        )
        return [(str(r.payload["text"]), str(r.channel)) for r in rows]


async def test_ask_me_first_waits_for_the_owners_answer(w: World) -> None:
    acct = await w.account("100", permission="ASK")
    await w.market("BUY")
    assert await sim.decide(w.db, w.clock.now()) == [f"{acct}:ASKED"]
    assert await sim.decide(w.db, w.clock.now()) == []  # once per candle: never asked twice
    assert await w.positions(acct) == []  # nothing opens before the answer
    (told,) = await _told(w)
    assert told[0].startswith("MARKET OPPORTUNITY (simulation, not sent to HFM): BUY")
    assert "in the portal" in told[0]
    async with w.db.connect() as conn:
        b = await board.build(conn, w.clock.now())
    assert b.pending is not None and b.status[0] == board.ATTENTION
    async with w.db.transaction() as conn:
        out = await sim.answer(conn, b.pending.id, True, w.owner, w.clock.now())
    assert out.startswith("opened: BUY 0.06 oz")  # 2.00 at a 30.00 stop
    (p,) = await w.positions(acct)
    assert p.side == "BUY" and p.ounces == D("0.06")
    with pytest.raises(ConflictError, match="already answered"):
        async with w.db.transaction() as conn:
            await sim.answer(conn, b.pending.id, True, w.owner, w.clock.now())
    async with w.db.connect() as conn:
        b = await board.build(conn, w.clock.now())
    assert b.pending is None and b.status[0] == board.RUNNING


async def test_a_rejected_or_late_answer_opens_nothing(w: World) -> None:
    acct = await w.account("100", permission="ASK")
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    async with w.db.connect() as conn:
        asked = await sim.pending_ask(conn, acct, w.clock.now())
    assert asked is not None
    async with w.db.transaction() as conn:
        assert await sim.answer(conn, asked.id, False, w.owner, w.clock.now()) == (
            "rejected: nothing opens"
        )
    assert await w.positions(acct) == []
    w.clock.advance(minutes=1)
    await w.market("BUY")
    await w.hfm("4040.00", "4040.30")
    await sim.decide(w.db, w.clock.now())
    async with w.db.connect() as conn:
        again = await sim.pending_ask(conn, acct, w.clock.now())
    assert again is not None and again.id != asked.id
    w.clock.advance(minutes=6)
    with pytest.raises(ConflictError, match="too late"):
        async with w.db.transaction() as conn:
            await sim.answer(conn, again.id, True, w.owner, w.clock.now())
    assert await w.positions(acct) == []
    async with w.db.connect() as conn:
        assert await sim.pending_ask(conn, acct, w.clock.now()) is None


async def test_an_approval_is_checked_against_the_limits_again(w: World) -> None:
    """Minutes pass between the question and the answer: a loss meanwhile that reaches the
    day's limit keeps the approved trade from opening."""
    acct = await w.account("100", permission="ASK")  # daily limit 10.00
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    async with w.db.connect() as conn:
        asked = await sim.pending_ask(conn, acct, w.clock.now())
    assert asked is not None
    async with w.db.transaction() as conn:  # a loss booked meanwhile, as a closed trade's
        await conn.execute(
            gold_sim_ledger.insert().values(
                id=uuid7(),
                account_id=acct,
                at=w.clock.now(),
                kind="RESULT",
                amount=D("-10.00"),
                ref=f"test-loss:{acct}",
                note="a loss meanwhile",
            )
        )
        out = await sim.answer(conn, asked.id, True, w.owner, w.clock.now())
    assert out == "not opened: today's loss (10.00) reached your daily limit of 10.00"
    assert await w.positions(acct) == []


async def test_automation_off_opens_nothing_and_keeps_the_stop_watched(w: World) -> None:
    acct = await w.account("100")
    await w.market("BUY")
    await sim.decide(w.db, w.clock.now())
    (p,) = await w.positions(acct)
    async with w.db.transaction() as conn:
        await sim.automation(conn, acct, False, w.clock.now())
    s = await _setting(w, acct)
    assert (await _status(w, acct), s.amount, s.risk_pct) == ("PAUSED", D(100), D(2))
    w.clock.advance(minutes=1)
    await w.market("BUY")
    assert await sim.decide(w.db, w.clock.now()) == []  # paused: no decision, no new trade
    await w.hfm(str(p.stop_price - 1), str(p.stop_price - D("0.7")))
    assert await sim.watch(w.db, w.clock.now()) == ["BUY:STOP_LOSS"]  # still protected
    async with w.db.transaction() as conn:
        await sim.automation(conn, acct, True, w.clock.now())
    assert await _status(w, acct) == "RUNNING"


async def test_the_status_is_said_from_the_evidence(w: World) -> None:
    async def status() -> str:
        async with w.db.connect() as conn:
            return (await board.build(conn, w.clock.now())).status[0]

    await w.market("BUY")
    assert await status() == board.READY
    acct = await w.account("100", allow_sell=False)
    assert await status() == board.WAITING  # started a moment ago, nothing decided yet
    await sim.decide(w.db, w.clock.now())
    assert await status() == board.RUNNING  # a trade open
    w.clock.advance(seconds=20)  # the bridge silent for over 15 s
    assert await status() == board.ATTENTION
    await w.hfm("4040.00", "4040.30")
    w.clock.advance(minutes=1)
    await w.hfm("4040.00", "4040.30")
    async with w.db.transaction() as conn:
        await sim.close_all(conn, acct, w.clock.now(), "test")
    w.clock.advance(minutes=4)
    await w.hfm("4040.00", "4040.30")
    assert await status() == board.ATTENTION  # market open, no decision for 4 minutes
    await w.hfm("4040.00", "4040.30", tick_age=timedelta(minutes=6))  # the market closed
    assert await status() == board.WAITING
    async with w.db.transaction() as conn:
        await sim.automation(conn, acct, False, w.clock.now())
    assert await status() == board.PAUSED
    async with w.db.transaction() as conn:
        await sim.set_running(conn, acct, False, "stopped by the owner", w.clock.now())
    assert await status() == board.STOPPED


def test_the_calculations_follow_the_amount_and_never_raise_the_risk() -> None:
    reading = analysis.Reading(
        at=WED, bar=WED, decision=analysis.WAIT, reasons=["x"], frames={}, stop_distance=D(30)
    )
    price = sim.Price(D("4040.00"), D("4040.30"), WED, D(1000), HFM_GOLD)
    small = board.calculate(sim.defaults(D(10)), sim.fresh_state(D(10)), reading, price)
    assert (small.risk_per_trade, small.daily_loss, small.at_risk_at_once) == (
        D("0.20"),
        D("0.60"),
        D("0.50"),
    )
    assert small.size is not None and small.size.ounces == 0
    assert small.size.why_not is not None
    assert small.size.why_not.startswith("Your selected trading amount is too small")
    assert "trades from an amount of 15.30" in small.size.why_not  # 0.01 oz × 30.60 at 2 %
    # HFM's 1 ounce: the 30.00 stop and the 0.1 % limit margin (4.04) a buy is sent with
    assert (small.hfm_risk, small.hfm_from) == (D("34.04"), D("1702.02"))
    assert small.swap_buy is not None and small.swap_buy < 0 and small.swap_sell == 0
    big = board.calculate(sim.defaults(D(1000)), sim.fresh_state(D(1000)), reading, price)
    assert big.size is not None and big.size.ounces == D("0.65")  # 20.00 / 30.60
    assert big.target_distance == D("61.20") and big.spread == D("0.30")


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


def test_the_timing_says_the_session_and_the_next_busy_window() -> None:
    from cosmic.gold import timing

    reading = analysis.Reading(
        at=WED,
        frames={"1h": analysis.Frame("1h", 40, atr_pct=0.005)},
        decision=analysis.WAIT,
        stop_distance=D(30),
    )
    # Wednesday 14 Oct 2026, 14:00 UTC: London (BST) and New York (EDT) are both open.
    t = timing.timing(WED, reading, D("0.30"), True)
    assert t.open_sessions == ("London", "New York") and t.suitability == timing.GOOD
    assert "busiest" in t.why and t.next_window == (
        datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 14, 16, 0, tzinfo=UTC),
    )
    # Early Thursday in Tokyo; the next overlap is that day's, in UTC.
    early = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
    t = timing.timing(early, reading, D("0.30"), True)
    assert t.open_sessions == ("Asia (Tokyo)",)
    assert t.next_window is not None and t.next_window[0] == datetime(
        2026, 10, 15, 12, 0, tzinfo=UTC
    )
    # After the clocks change (London 25 Oct, New York 1 Nov), the window moves with them.
    winter = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    t = timing.timing(winter, reading, D("0.30"), True)
    assert t.next_window == (
        datetime(2026, 11, 4, 13, 0, tzinfo=UTC),
        datetime(2026, 11, 4, 17, 0, tzinfo=UTC),
    )
    saturday = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    t = timing.timing(saturday, reading, None, False)
    assert t.suitability == timing.CLOSED and "weekly break" in t.why
    assert t.next_window is not None and t.next_window[0].weekday() == 0  # Monday's
    assert timing.timing(WED, reading, D(4), True).suitability == timing.COSTLY  # 4 of a 30 stop
    quiet = analysis.Reading(
        at=WED, frames={"1h": analysis.Frame("1h", 40, atr_pct=0.0001)}, decision=analysis.WAIT
    )
    assert timing.timing(WED, quiet, D("0.30"), True).suitability == timing.QUIET
    assert "not checked" in t.news
