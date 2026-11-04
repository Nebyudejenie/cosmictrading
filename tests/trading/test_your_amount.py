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
from cosmic.db.schema_market import fetch_runs, instruments, sources
from cosmic.reporting import hfm_settings as gold
from cosmic.trading import mandates
from cosmic.trading.mandates import Usage, entry_refusal, terms_from_money
from cosmic.trading.policy import active_policy, change_policy
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
    assert not t.ok and t.why == (
        "WAIT — Your selected trading amount is too small for this opportunity at your chosen "
        "risk level: the smallest trade (0.01) would risk about 0.30, more than the 0.20 allowed "
        "per trade"
    )
    assert t.fix == ["At 2 %, it trades from an amount of 15.00"]  # never a higher risk
    assert sim_3.ok and sim_3.ounces == D("0.01")  # and at 3 % it trades
    # The HFM demo: HFM's own smallest trade, 0.01 lot = 1 ounce, would lose 30 at the stop.
    assert not any(demo[a].ok for a in AMOUNTS)
    d = demo["200"]
    assert (d.smallest, d.smallest_loss, d.contract) == (D(1), D("30.00"), D(100))
    assert d.fix == [
        "At 2 %, it trades from an amount of 1,500.00",
        "The internal simulation trades from 0.01 ounce on HFM's prices "
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
