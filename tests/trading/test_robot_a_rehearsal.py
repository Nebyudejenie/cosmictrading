"""Robot A rehearsed as production stood on 2026-11-21 (S112), on two MT5 emulators: its own
demo holds 100 USD (HFM's 1:1000), gold near 3,575, and the server's risk policy is the one
production runs (version 6, copied value for value), with only what START adds to it. From the
owner's START when ready to HFM's own target, then three losses and the day's stop (learning mode
on the demo since S124: it trades again by itself the next day): everything the owner will see,
before the market opens."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.db.schema_trading import accounts
from cosmic.gold import controls, robot_setup, trend_test
from cosmic.gold import jobs as gold_jobs
from cosmic.trading.brokers.mt5_bridge import BRIDGE, BRIDGE_2
from cosmic.trading.policy import change_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, principal_for
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_daily import _later, _two_demos
from tests.trading.test_gold_hfm_demo import _cycles
from tests.trading.test_gold_trend_test import _closed, _decide, _filled
from tests.trading.test_hfm import Hfm, next_report, open_account, paxg
from tests.trading.test_hfm_two_bridges import bridges_running

pytestmark = pytest.mark.keeps_binance_paused  # the gold robots never read Binance's prices

# Production's active risk policy, version 6, read on 2026-11-21 (limits only).
PRODUCTION: dict[str, Any] = {
    "allowed_instruments": [
        "SPOT:BTC/USDT@BINANCE",
        "SPOT:PAXG/USDT@BINANCE",
        "SPOT:BTC/USDT@BINANCE_TESTNET",
        "CFD:XAU/USD@HFM",
        "CFD:BTC/USD@HFM",
        "CFD:XAU/USD@HFM_CENT",
    ],
    "allowed_venues": ["BINANCE", "BINANCE_TESTNET", "HFM", "HFM_CENT"],
    "allowed_quote_currencies": ["USDT", "USD"],
    "allowed_order_types": ["LIMIT"],
    "allowed_time_in_force": ["GTC", "GTD", "IOC"],
    "allow_short": False,
    "max_leverage": "1",
    "max_order_notional": "5000",
    "max_position_notional": "10000",
    "max_gross_exposure": "20000",
    "max_concentration_pct": "50",
    "max_daily_loss": "100",
    "max_drawdown_pct": "20",
    "max_loss_streak": 10,
    "max_open_orders": 10,
    "max_open_positions": 3,
    "max_open_risk_pct": "4",
    "max_price_age_seconds": 120,
    "allow_delayed_data": True,
    "max_spread_bps": "25",
    "require_cross_source_check": True,
    "max_cross_source_deviation_bps": "50",
    "require_venue_status": True,
    "max_slippage_bps": "20",
    "max_cost_bps": "100",
    "rounding_tolerance_bps": "10",
    "fee_bps_assumption": "60",
    "venue_fees": [
        {"venue": "BINANCE", "bps": "10"},
        {"venue": "HFM", "bps": "0"},
        {"venue": "HFM_CENT", "bps": "0"},
    ],
    "short_venues": ["HFM"],
    "margin_venues": [{"venue": "HFM", "max_leverage": "50"}],
    "account_limits": [],
    "approval_ttl_seconds": 900,
    "intent_ttl_seconds": 3600,
    "reconciliation_max_age_seconds": 900,
    "blackout_windows": [],
    "risk_per_trade_pct": "1",
    "uncertainty_haircut_pct": "25",
    "protective_retry_max": 2,
    "emergency": {
        "cancel_unfilled_entries": True,
        "cancel_protective_orders": False,
        "liquidate_positions": False,
    },
}

BID, ASK = "3575.00", "3575.35"  # HFM's gold on 2026-11-20 (spread 0.35)
FALLING_HOURS = [3975.0 - k for k in range(400)]
TURN_DOWN = [3575.0] * 60 + [3575 + 1.5 * k for k in range(1, 8)] + [3575.0]


def _hundred(emu: MT5Emulator) -> MT5Emulator:
    """A demo of 100 USD with HFM's gold where it traded on 2026-11-20."""
    emu.balance = D(100)
    emu.known[(emu.number, emu.server)]["balance"] = D(100)
    emu.book["XAUUSD"] = (D(BID), D(ASK))
    return emu


async def _as_production(h: Hfm) -> None:
    async with h.env.db.transaction() as conn:
        await change_policy(
            conn,
            principal=principal_for(h.env.owner, h.env.clock),
            new_params=PRODUCTION,
            reason="as production",
            now=h.env.clock.now(),
            settings=h.env.settings,
        )


async def _signal(h: Hfm, emus: tuple[MT5Emulator, MT5Emulator], w: Any) -> list[str]:
    """A SELL signal in a volatile market (its record: 34.7 %, +0.200 R), 15 minutes on: gold
    back at 3,575 and Binance's PAXG 15 bps under it (its usual gap, under the policy's 50)."""
    _later(h, emus, minutes=15)
    for e in emus:
        e.book["XAUUSD"] = (D(BID), D(ASK))
    await paxg(h.env.db, h.env.clock, "3569.40", "3569.80")
    await w.candles("1h", FALLING_HOURS, 3.0)
    await w.candles("15m", TURN_DOWN, 4.8)
    await next_report(emus[1])
    return await _decide(h)


async def _decisions(h: Hfm) -> list[Any]:
    async with h.env.db.connect() as conn:
        return list(
            await conn.execute(select(gold_hfm_decisions).order_by(gold_hfm_decisions.c.at))
        )


async def _settled(h: Hfm, emu: MT5Emulator, account: Any) -> None:
    """HFM's answer stored, and the account reconciled (production: every 5 minutes)."""
    await next_report(emu)
    await _cycles(h)
    report = await h.env.svc.reconciler.reconcile(account, trigger="t")
    assert report.mismatches == ()


async def test_robot_a_trades_its_100_usd_demo_under_productions_own_limits(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, _, emu1, emu2, w = await _two_demos(settings, db)
    _hundred(emu1), _hundred(emu2)
    emus = (emu1, emu2)
    async with bridges_running(
        db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path, lanes=True
    ):
        await open_account(h, capital="100")  # Robot B's demo, as in production
        await _as_production(h)
        # The owner's one step: START when ready, with their code.
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/gold/sim")).text
            r = await c.post("/gold/test/arm", data={"csrf_token": csrf_from(page)})
            assert r.status_code == 303
        # The market opens: the worker's step checks terminal 2, opens its account, starts
        # Robot A, and has the account reconciled at once (S112).
        await next_report(emu2)
        assert await gold_jobs.setup_now(h.ctx) == "started"
        async with db.connect() as conn:
            acct = (
                await conn.execute(select(accounts).where(accounts.c.alias == robot_setup.ALIAS))
            ).one()
            (m,) = await trend_test.test_mandates(conn, h.env.clock.now())
        assert (acct.kind, m.account_id, m.capital) == ("SANDBOX", acct.id, 100)
        assert acct.reconciliation_status == "OK"  # its first signal is not refused (R03)

        # 1. A SELL signal: 1 ounce sent at once, HFM holding the stop and the 1:2 target.
        sent = await _signal(h, emus, w)
        assert sent == [f"{m.id}:PROPOSED"], (await _decisions(h))[-1].reasons
        d = (await _decisions(h))[-1]
        assert (d.decision, d.ounces) == ("SELL", 1)
        assert D(17) < d.stop_distance + trend_test.SLIPPAGE <= 30
        await _filled(emu2)
        (pos,) = emu2.positions.values()
        assert pos.volume == D("0.01") and pos.type == 1
        assert not emu1.positions  # Robot B's terminal untouched
        await _settled(h, emu2, acct.id)
        # HFM's own target is reached: the trade closes at HFM, about 2 R won.
        emu2.move("XAUUSD", str(pos.tp - D("0.40")), str(pos.tp - D("0.05")))
        await _closed(emu2)
        await _settled(h, emu2, acct.id)
        won = emu2.balance - 100
        assert won > D("1.9") * d.stop_distance

        # 2 and 3. Two signals that hit their stops.
        for _ in range(2):
            sent = await _signal(h, emus, w)
            assert sent == [f"{m.id}:PROPOSED"], (await _decisions(h))[-1].reasons
            await _filled(emu2)
            (pos,) = emu2.positions.values()
            emu2.move("XAUUSD", str(pos.sl + D("0.05")), str(pos.sl + D("0.40")))
            await _closed(emu2)
            await _settled(h, emu2, acct.id)

        # 4. The next signal still goes (99 USD left: within every limit); its stop is hit too.
        sent = await _signal(h, emus, w)
        assert sent == [f"{m.id}:PROPOSED"], (await _decisions(h))[-1].reasons
        await _filled(emu2)
        (pos,) = emu2.positions.values()
        emu2.move("XAUUSD", str(pos.sl + D("0.05")), str(pos.sl + D("0.40")))
        await _closed(emu2)
        await _settled(h, emu2, acct.id)

        # 5. Three losses in a row today: learning mode on the demo (S124, the owner's choice of
        # 2026-11-28) stops Robot A for the rest of the day; it is never paused.
        assert await _signal(h, emus, w) == [f"{m.id}:WAIT"]
        last = (await _decisions(h))[-1]
        assert last.reasons[-1].startswith(
            "WAIT — stopped for today: 3 losing trades in a row today (your limit is 3)"
        )
        assert D(70) < emu2.balance < 100  # a win of about 40, three losses of about 20
        async with db.connect() as conn:
            ctl = await controls.get(conn, m.id)
            told = list(
                await conn.execute(
                    select(notifications.c.kind, notifications.c.payload["text"].astext).order_by(
                        notifications.c.created_at
                    )
                )
            )
        assert ctl is not None and ctl.status == controls.RUNNING
        kinds = [k for k, _ in told]
        # Each trade is told: sent, filled, closed (one at HFM's own target); no pause.
        assert kinds.count("auto_trade") == 4 and kinds.count("closed_at_take_profit") == 1
        assert kinds.count("trade_closed") == 4
        assert not any("PAUSED" in (t or "") for _, t in told)
        assert not any("HFM DEMO robot" in (t or "") for _, t in told)
        assert not emu2.positions and not emu1.positions  # Robot B's terminal never touched
