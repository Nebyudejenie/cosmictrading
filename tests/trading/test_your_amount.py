"""You decide the amount; the system sizes the gold (S60, the owner's correction of 2026-11-04).

The auto-trader skipped every signal on the HFM demo because its money had to cover the whole
value of 1 ounce (ADR-028, fully paid) and a trade had to risk at least 1.00. Now gold is held on
margin, up to 10 times the amount, each trade sized by the loss at its stop; the internal
simulation trades from 0.01 ounce on HFM's prices; and the preview shows the size before
anything starts. Gold here: HFM's 4,100.00 / 4,100.30; hourly bars with a range of 10, so
ATR(20) = 10 and the stop is 30 an ounce below a buy at 4,104.40 (the ask + 0.1 %)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders, trade_intents
from cosmic.db.schema_market import fetch_runs, instruments, sources
from cosmic.reporting import hfm_home
from cosmic.reporting import hfm_settings as gold
from cosmic.research.library import auto
from cosmic.trading import ledger, mandates
from cosmic.trading.accounts import get_account
from cosmic.trading.mandates import Usage, entry_refusal, terms_from_money
from cosmic.trading.policy import active_policy, change_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_hfm import Hfm, bridge_running, build, open_account, paxg
from tests.trading.test_hfm_test_trade import _snapshot_until, _weekday

SIM = "CFD:XAU/USD@HFM_CENT"
WHEN = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
AMOUNTS = ("10", "20", "50", "100", "200")


# ---- the rules, pure -------------------------------------------------------------------------


def test_any_amount_from_a_cent_of_risk() -> None:
    """No 1.00 floor: $10 at 2 % may lose 0.20 a trade, 0.60 a day, 2.00 in all."""
    t = terms_from_money(
        account_alias="hfm-sim", money=D(10), care="BOLD", mode="AUTO", instruments=(SIM,)
    )
    assert (t.max_loss_per_trade, t.daily_loss_limit, t.total_limit) == (
        D("0.20"),
        D("0.60"),
        D("2.00"),
    )
    t = terms_from_money(
        account_alias="hfm-sim",
        money=D(50),
        care="NORMAL",
        mode="ASK",
        instruments=(SIM,),
        risk_pct=D("3.5"),
        open_positions=3,
        daily_loss=D("4"),
    )
    assert (t.max_loss_per_trade, t.max_open_positions, t.daily_loss_limit) == (
        D("1.75"),
        3,
        D("4"),
    )
    for wrong in ({"risk_pct": D(6)}, {"open_positions": 6}, {"daily_loss": D("0.10")}):
        with pytest.raises(Exception, match="must be"):
            terms_from_money(
                account_alias="hfm-sim",
                money=D(50),
                care="NORMAL",
                mode="AUTO",
                instruments=(SIM,),
                **wrong,  # type: ignore[arg-type]
            )


def test_gold_is_held_on_margin_up_to_ten_times_the_amount() -> None:
    assert mandates.exposure((SIM,)) == 10 and mandates.exposure(("CFD:XAU/USD@HFM",)) == 10
    assert mandates.exposure(("SPOT:PAXG/USDT@BINANCE",)) == 1  # a coin stays fully paid
    t = terms_from_money(
        account_alias="hfm-demo",
        money=D(1000),
        care="BOLD",
        mode="AUTO",
        instruments=("CFD:XAU/USD@HFM",),
    )
    m = mandates.Mandate(
        id=uuid7(),
        account_id=uuid7(),
        strategy_key="turtle_20_10",
        timeframe="1h",
        instruments=t.instruments,
        max_loss_per_trade=t.max_loss_per_trade,
        max_trades_per_day=t.max_trades_per_day,
        max_open_positions=t.max_open_positions,
        daily_loss_limit=t.daily_loss_limit,
        status="ACTIVE",
        granted_by=uuid7(),
        granted_at=WHEN,
        expires_at=WHEN + timedelta(days=30),
        ended_at=None,
        end_reason=None,
        capital=t.capital,
        risk_pct=t.risk_pct,
    )
    u = Usage(0, 0, D(0))
    assert entry_refusal(m, u, D(20), D("4104.40")) is None  # 1 oz: was "worth more than 1,000"
    refused = entry_refusal(m, Usage(0, 0, D(0), D(8000)), D(20), D("4104.40"))
    assert refused is not None and "more than 10 times your 1,000.00 (10,000.00)" in refused


# ---- the preview, on HFM's prices --------------------------------------------------------------


async def _gold_hours(h: Hfm, closes: list[float], *, half_range: float = 5) -> None:
    """Completed hourly bars of the gold series the auto-trader reads, up to the last hour."""
    now = h.env.clock.now()
    last = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    async with h.env.db.transaction() as conn:
        inst = await conn.scalar(
            select(instruments.c.id).where(
                instruments.c.canonical_key == mandates.AUTO_SIGNAL_SERIES[SIM]
            )
        )
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == "binance_public"))
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run, source_id=src, kind="t", started_at=now, finished_at=now, status="SUCCESS"
            )
        )
        first = last - timedelta(hours=len(closes) - 1)
        for i, c in enumerate(closes):
            await conn.execute(
                text(
                    "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, "
                    "low, close, volume, is_complete, fetch_run_id, ingested_at, updated_at) "
                    "VALUES (:i, :s, '1h', :t, :c, :h, :l, :c, 1, true, :r, :n, :n) "
                    "ON CONFLICT (instrument_id, source_id, timeframe, open_time) DO UPDATE SET "
                    "open = :c, high = :h, low = :l, close = :c"
                ),
                {
                    "i": inst,
                    "s": src,
                    "t": first + timedelta(hours=i),
                    "c": D(str(c)),
                    "h": D(str(c + half_range)),
                    "l": D(str(c - half_range)),
                    "r": run,
                    "n": now,
                },
            )


def quiet(n: int = 80) -> list[float]:
    return [4100.0 + (1 if i % 2 else -1) for i in range(n)]


async def _allow_simulation(h: Hfm) -> None:
    """The owner's policy: gold on HFM and on the simulation's Cent terms, no commission."""
    async with h.env.db.transaction() as conn:
        _, policy = await active_policy(conn)
        params = policy.model_dump()
        params.update(
            allowed_instruments=("CFD:XAU/USD@HFM", SIM),
            allowed_venues=("HFM", "HFM_CENT"),
            venue_fees=({"venue": "HFM", "bps": D(0)}, {"venue": "HFM_CENT", "bps": D(0)}),
        )
        await change_policy(
            conn,
            principal=h.env.principal(),
            new_params=params,
            reason="the gold auto-trader: HFM and the simulation",
            now=h.env.clock.now(),
            settings=h.env.settings,
        )


async def _preview(h: Hfm, mode: str, amount: str, risk: str = "2") -> gold.Preview:
    async with h.env.db.connect() as conn:
        return await gold.preview(
            conn,
            h.env.clock.now(),
            gold.Choice(mode=mode, amount=D(amount), risk_pct=D(risk)),
        )


async def test_the_size_follows_the_amount_in_the_simulation_and_on_the_hfm_demo(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        await _allow_simulation(h)
        await _gold_hours(h, quiet())
        sim = {a: await _preview(h, gold.SIMULATION, a) for a in AMOUNTS}
        demo = {a: await _preview(h, gold.HFM_DEMO, a) for a in AMOUNTS}
        big = await _preview(h, gold.HFM_DEMO, "1500")
        sim_3 = await _preview(h, gold.SIMULATION, "10", "3")

    s = sim["50"]
    assert (s.atr, s.entry, s.stop, s.stop_distance) == (D(10), D("4104.40"), D("4074.40"), D(30))
    assert (s.max_loss, s.smallest, s.step) == (D("1.00"), D("0.01"), D("0.01"))
    # The simulation (HFM Cent rules, 0.01 ounce steps): the size follows the amount.
    assert [sim[a].ounces for a in AMOUNTS] == [D(0), D("0.01"), D("0.03"), D("0.06"), D("0.13")]
    assert [sim[a].loss_at_stop for a in AMOUNTS] == [
        D(0),
        D("0.30"),
        D("0.90"),
        D("1.80"),
        D("3.90"),
    ]
    assert all(sim[a].loss_at_stop <= sim[a].max_loss for a in AMOUNTS)
    assert (s.value, s.lots, s.margin, s.cap) == (D("123.13"), D("0.03"), D("0.12"), D(500))
    assert s.spread_cost == D("0.01") and s.fees == D(0)
    # $10 at 2 % may lose 0.20; the smallest trade would lose 0.30: said, with what would fit.
    t = sim["10"]
    assert not t.ok and t.why.startswith("the smallest trade (0.01) would risk about 0.30")
    assert t.fix == [
        "a risk of 3.00 % per trade (instead of 2 %)",
        "an amount of 15.00 or more at 2 %",
    ]
    assert sim_3.ok and sim_3.ounces == D("0.01")  # and at 3 % it trades
    # The HFM demo: HFM's own smallest trade, 0.01 lot = 1 ounce, would lose 30 at the stop.
    assert not any(demo[a].ok for a in AMOUNTS)
    d = demo["200"]
    assert (d.smallest, d.smallest_loss, d.contract) == (D(1), D("30.00"), D(100))
    assert d.fix == [
        "an amount of 1,500.00 or more at 2 %",
        "or the internal simulation, which trades from 0.01 ounce on HFM's prices "
        "(about 0.30 at the stop, before costs)",
    ]
    assert big.ok and (big.ounces, big.lots, big.loss_at_stop) == (D(1), D("0.01"), D("30.00"))
    assert (big.value, big.margin, big.leverage) == (D("4104.40"), D("4.10"), D(1000))
    assert big.free_margin == D(100_000) and big.balance == D(100_000)


async def _as_on_the_server(h: Hfm, account_id: Any) -> None:
    """What the server does every few minutes: HFM's report, the reference price for the
    cross-check (R10) and the check of the account (R03)."""
    h.emu.now_ms = int(h.env.clock.now().timestamp() * 1000)
    await _snapshot_until(h, lambda s: True)
    await paxg(h.env.db, h.env.clock, "4100.10", "4100.40")
    await h.env.svc.reconciler.reconcile(account_id, trigger="t")


async def test_a_simulated_trade_from_your_amount_buys_closes_and_counts(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """$50 at 2 %, started with the owner's code: a breakout buys 0.03 ounce on the simulation
    (never at HFM), the rule's exit sells it, and the result is what the prices made."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        await _allow_simulation(h)
        await _gold_hours(h, quiet())
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            await _snapshot_until(h, lambda s: True)
            page = (await c.get("/hfm/settings?mode=SIMULATION&amount=50&risk=2")).text
            assert "✓ A trade fits." in page and "0.03 ounce" in page and "SIMULATED" in page
            r = await c.post(
                "/hfm/settings",
                data={
                    "csrf_token": csrf_from(page),
                    "mode": "SIMULATION",
                    "amount": "50",
                    "risk": "2",
                    "open_positions": "1",
                    "automation": "AUTO",
                },
            )
            assert r.status_code == 303
        async with db.connect() as conn:
            account = await get_account(conn, alias="hfm-sim")
            (m,) = [
                x
                for x in await mandates.active_mandates(conn, h.env.clock.now())
                if x.account_id == account.id
            ]
        assert (account.kind, account.venue_key) == ("PAPER", "HFM_CENT")
        assert (m.capital, m.max_loss_per_trade, m.instruments) == (D(50), D("1.00"), (SIM,))
        await h.env.svc.reconciler.reconcile(account.id, trigger="t")
        sent = len(emu.sent)
        # The HFM panel says where the gold auto-trader runs, and asks nothing.
        async with db.connect() as conn:
            home = await hfm_home.build(conn, h.env.clock.now())
        running = next(a for a in home.answers if a.question == "Is automatic trading running?")
        assert running.ok is None and "internal simulation (SIMULATED) with 50.00" in running.text
        assert not any("Start the gold auto-trader" in t for t in home.attention)

        # WAIT: quiet hours, no signal.
        assert await auto.run(db, h.env.clock) == []
        # BUY: the last hour closes above the 20-hour high.
        h.env.clock.advance(hours=1)
        await _as_on_the_server(h, account.id)
        await _gold_hours(h, [*quiet(), 4115.0], half_range=5)
        got = await auto.run(db, h.env.clock)
        async with db.connect() as conn:
            why = [r.status_reason for r in await conn.execute(select(trade_intents))]
        assert got == [f"{SIM}:APPROVED"], why
        await h.env.cycle(2)
        async with db.connect() as conn:
            entry = (
                await conn.execute(
                    select(orders).where(
                        orders.c.account_id == account.id, orders.c.purpose == "ENTRY"
                    )
                )
            ).one()
        assert entry.status == "FILLED" and entry.is_simulated
        assert entry.filled_qty == D("0.03")  # 1.00 / about 31.5 an ounce at the stop
        assert len(emu.sent) == sent  # nothing went to HFM

        # SELL: two hours on, gold closes below the 10-hour low; the rule's exit sells.
        h.env.clock.advance(hours=2)
        emu.move("XAUUSD", "4108.00", "4108.30")
        await _as_on_the_server(h, account.id)
        await _snapshot_until(h, lambda s: D(str(s["ticks"]["XAUUSD"]["bid"])) == D("4108"))
        await _gold_hours(h, [*quiet(), 4115.0, 4112.0, 4090.0], half_range=5)
        assert await auto.run(db, h.env.clock) == [f"{SIM}:APPROVED"]
        await h.env.cycle(2)
        async with db.connect() as conn:
            sides = [
                (r.side, r.status)
                for r in await conn.execute(
                    select(trade_intents)
                    .where(trade_intents.c.account_id == account.id)
                    .order_by(trade_intents.c.created_at)
                )
            ]
            exit_ = (
                await conn.execute(
                    select(orders).where(
                        orders.c.account_id == account.id, orders.c.purpose == "EXIT"
                    )
                )
            ).one()
            cash = await ledger.book_balance(conn, account.id, "CASH", "USD")
        assert [s for s, _ in sides] == ["BUY", "SELL"] and exit_.status == "FILLED"
        # The result is the prices' own: bought at the ask, sold at the bid, 0.03 ounce.
        made = (exit_.avg_fill_price - entry.avg_fill_price) * D("0.03")
        assert cash == gold.SIM_CAPITAL + made - entry.fees_paid - exit_.fees_paid
        # The simulator's fills: the ask and the bid, each 0.05 % worse (its slippage).
        assert entry.avg_fill_price == D("4100.30") * D("1.0005")  # 4,102.35015
        assert exit_.avg_fill_price == D("4108.00") * D("0.9995")  # 4,105.946
        assert made == D("0.1078755")  # (4,105.946 - 4,102.35015) x 0.03 ounce, no commission
