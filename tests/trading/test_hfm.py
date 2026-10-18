"""HFM end to end (ADR-028): bridge code on the MT5 emulator, the sync, the HFM adapter, the
execution engine and reconciliation — contract check, account opening, buy, stop on the
position, owner exit, stop-out with swap."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from bridge.cosmic_mt5_bridge import Bridge, Config, Journal
from sqlalchemy import func, select

from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.db.schema_execution import fills, orders
from cosmic.db.schema_market import fetch_runs, instruments, quotes, sources
from cosmic.db.schema_trading import bridge_commands, bridge_state, positions
from cosmic.market import data as md
from cosmic.market.jobs import check_source_freshness
from cosmic.notify.telegram.format import proposal_summary
from cosmic.reporting.daily import health_lines
from cosmic.sources.ingest import ensure_registry
from cosmic.trading import authority, ledger
from cosmic.trading.accounts import get_account
from cosmic.trading.bridge_sync import BridgeSync, apply_sync
from cosmic.trading.brokers.base import BrokerUnavailableError
from cosmic.trading.brokers.hfm_check import CONTRACT_VERSION, run_contract
from cosmic.trading.brokers.mt5_bridge import ADAPTER, HfmBridgeAdapter, hfm_symbols
from cosmic.trading.exits import propose_close
from cosmic.trading.jobs import open_sandbox
from cosmic.trading.modes import Mode
from cosmic.trading.policy import active_policy, change_policy, ensure_default_policy
from cosmic.trading.proposals import latest_packet
from cosmic.trading.sandbox import record_contract_run
from cosmic.trading.services import TradingServices
from cosmic.worker.context import WorkerContext
from tests.helpers import make_user, principal_for
from tests.mt5_emulator import MT5Emulator
from tests.trading_env import CAPTURE, Env

XAU = "CFD:XAU/USD@HFM"


@dataclass
class Hfm:
    env: Env
    emu: MT5Emulator
    adapter: HfmBridgeAdapter
    ctx: WorkerContext

    async def book(self, book: str, asset: str) -> D:
        async with self.env.db.connect() as conn:
            return await ledger.book_balance(conn, self.env.account_id, book, asset)

    async def reconcile(self) -> tuple[str, ...]:
        return (
            await self.env.svc.reconciler.reconcile(self.env.account_id, trigger="t")
        ).mismatches


async def paxg(db: Database, clock: FrozenClock, bid: str, ask: str) -> None:
    """A fresh PAXG/USDT quote: the reference that checks HFM's gold price."""
    now = clock.now()
    async with db.transaction() as conn:
        inst = await conn.scalar(
            select(instruments.c.id).where(instruments.c.canonical_key == "SPOT:PAXG/USDT@BINANCE")
        )
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == "binance_public"))
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run, source_id=src, kind="t", started_at=now, finished_at=now, status="SUCCESS"
            )
        )
        await conn.execute(
            quotes.insert().values(
                id=uuid7(),
                instrument_id=inst,
                source_id=src,
                bid=D(bid),
                ask=D(ask),
                last=D(bid),
                source_ts=now,
                ingested_at=now,
                fetch_run_id=run,
            )
        )


@contextlib.asynccontextmanager
async def bridge_running(
    db: Database, clock: FrozenClock, emu: MT5Emulator, tmp: Path
) -> AsyncIterator[Bridge]:
    """The Windows bridge's loop, in process: snapshot → sync → run commands, every 20 ms."""
    cfg = Config("https://x.invalid", "hfm-demo", "terminal64.exe", ("XAUUSD", "#BTCUSD"), "t")
    bridge = Bridge(
        emu, cfg, lambda p: {}, Journal(tmp / "journal.jsonl"), clock=lambda: clock.now()
    )

    async def loop() -> None:
        while True:
            payload = bridge.snapshot()
            payload["results"] = list(bridge.journal.unsent)
            sync = BridgeSync.model_validate(json.loads(json.dumps(payload)))
            commands = await apply_sync(db, clock, sync)
            bridge.journal.unsent = []
            for c in commands:
                bridge.journal.record(bridge.run_command(c))
            await asyncio.sleep(0.02)

    task = asyncio.create_task(loop())
    try:
        for _ in range(1000):  # until the first report has arrived, as it has in real use
            async with db.connect() as conn:
                if (await conn.execute(select(bridge_state.c.bridge))).first() is not None:
                    break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("the bridge never reported")
        yield bridge
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def build(settings: Settings, db: Database, emu: MT5Emulator) -> Hfm:
    clock = FrozenClock(CAPTURE)
    emu.now_ms = int(CAPTURE.timestamp() * 1000)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    owner = await make_user(db, clock)
    adapter = HfmBridgeAdapter(
        db, clock, hfm_symbols(), wait=timedelta(seconds=10), poll_seconds=0.02
    )
    svc = TradingServices.build(db, clock, settings, hfm=adapter)
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="test")
    ctx.services["trading"] = svc
    p = principal_for(owner, clock)
    async with db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=p,
            target=Mode.PAPER_TRADING,
            reason="HFM demo for tests",
            now=clock.now(),
            settings=settings,
        )
        _, policy = await active_policy(conn)
        params = policy.model_dump()
        params.update(
            allowed_instruments=(XAU,),
            allowed_venues=("HFM",),
            allowed_quote_currencies=("USD",),
            max_order_notional=D(10_000),
            max_position_notional=D(20_000),
            max_gross_exposure=D(40_000),
        )
        await change_policy(
            conn,
            principal=p,
            new_params=params,
            reason="allow HFM gold for tests",
            now=clock.now(),
            settings=settings,
        )
    await paxg(db, clock, "4100.10", "4100.40")
    env = Env(settings, db, clock, svc, owner, uuid7())
    return Hfm(env, emu, adapter, ctx)


async def open_account(h: Hfm, capital: str = "20000") -> None:
    steps = await run_contract(h.adapter)
    assert [s.name for s in steps if not s.passed] == [], steps
    assert "the resting order carries its stop-loss" in [s.name for s in steps]
    async with h.env.db.transaction() as conn:
        await record_contract_run(
            conn,
            steps=steps,
            key_fingerprint=await h.adapter.account_fingerprint(),
            code_version=CONTRACT_VERSION,
            started_at=h.env.clock.now(),
            finished_at=h.env.clock.now(),
            actor=Actor.system("test"),
            adapter=ADAPTER,
        )
    account_id = await open_sandbox(
        h.ctx,
        {
            "alias": "hfm-demo",
            "managed_capital": capital,
            "user_id": str(h.env.owner.user_id),
            "adapter": ADAPTER,
        },
    )
    assert account_id is not None
    h.env.account_id = account_id


async def propose_gold(h: Hfm) -> Any:
    r = await h.env.propose(
        account_alias="hfm-demo",
        instrument_key=XAU,
        quantity=D("1"),
        limit_price=D("4101.00"),
        price_band_low=D("4090.00"),
        price_band_high=D("4110.00"),
        management_plan={"stop_loss": {"stop_price": D("4000.00"), "limit_price": D("3990.00")}},
        entry_conditions="Buy 1 oz at or below 4,101.",
    )
    assert r.status == "PROPOSED", r.problems
    await h.env.approve(r.intent_id)
    return r


async def buy_gold(h: Hfm) -> Any:
    r = await propose_gold(h)
    for _ in range(3):  # entry sent and filled; fill imported; stop recorded on the position
        await h.env.svc.execution.run_cycle()
    return r


async def test_contract_check_and_opening(settings: Settings, db: Database, tmp_path: Path) -> None:
    emu = MT5Emulator(server_offset_ms=3 * 3_600_000)  # HFM's clock: UTC+3
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        async with db.connect() as conn:
            acct = await get_account(conn, account_id=h.env.account_id)
        assert (acct.kind, acct.venue_key, acct.broker_adapter) == ("SANDBOX", "HFM", ADAPTER)
        assert acct.reconciliation_status == "OK"
        assert await h.book("CASH", "USD") == D("20000")
        assert await h.book("UNMANAGED", "USD") == D("80000")
        assert not emu.orders  # the check's resting order was cancelled
        async with db.connect() as conn:
            cancels = await conn.scalar(
                select(func.count())
                .select_from(bridge_commands)
                .where(bridge_commands.c.kind == "CANCEL")
            )
        assert cancels == 1  # once confirmed, the clean-up does not cancel again


async def test_buy_stop_and_exit_through_the_bridge(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        entry = await buy_gold(h)
        async with db.connect() as conn:
            packet = (await latest_packet(conn, entry.intent_id)).packet
            rows = {
                o.purpose: o
                for o in await conn.execute(
                    select(orders).where(orders.c.intent_id == entry.intent_id)
                )
            }
            held = await conn.scalar(
                select(positions.c.quantity).where(positions.c.account_id == h.env.account_id)
            )
        # What the owner approved describes HFM as it is: a demo account, a CFD in the broker's
        # hours with an overnight swap, and a broker stop that fills at the market price.
        assert packet["account"]["environment"] == "DEMO"
        assert packet["plain_language"]["account"] == (
            "hfm-demo (DEMO — HFM MetaTrader 5 demo account, virtual funds)"
        )
        assert packet["instrument"]["asset_class"] == "CFD"
        assert "weekends" in packet["instrument"]["session"]
        assert "long -78.56, short 0.0 per lot" in packet["costs"]["funding_borrow"]
        assert packet["management_plan"]["stop_loss"]["type"].startswith("broker stop-loss")
        assert "gap" in packet["risk"]["assumptions"]
        assert rows["ENTRY"].status == "FILLED" and rows["ENTRY"].is_simulated
        assert held == D("1")
        (pos,) = emu.positions.values()
        assert pos.sl == D("4000.00") and rows["STOP_LOSS"].status in ("ACKNOWLEDGED", "SUBMITTED")
        deal = next(r for r in emu.sent if r["action"] == emu.TRADE_ACTION_DEAL)
        assert deal["sl"] == 4000.0  # the stop travelled with the buy itself
        assert "HFM holds it" in packet["management_plan"]["atomic_bracket"]
        assert packet["strategy"]["name"] == "manual proposal by the operator"
        card = proposal_summary(entry.trade_code, packet, "PROPOSED")
        assert "Stop-loss: 4000 · take-profit: none" in card  # packets keep canonical decimals
        assert "% of equity)" in card and "Risk checks: " in card
        assert await h.book("CASH", "USD") == D("20000") - D("4100.30") - D("0.03")
        assert await h.reconcile() == ()
        # The owner closes it: the stop is removed first, then the long is sold (IOC).
        async with db.transaction() as conn:
            acct = await get_account(conn, account_id=h.env.account_id)
            inst = await md.instrument_by_key(conn, XAU)
            exit_ = await propose_close(
                conn,
                account=acct,
                inst=inst,
                reason="close the HFM test position",
                actor=Actor.user(h.env.owner.user_id),
                now=h.env.clock.now(),
            )
        assert exit_.status == "PROPOSED", exit_.problems
        await h.env.approve(exit_.intent_id)
        for _ in range(4):
            await h.env.svc.execution.run_cycle()
        assert not emu.positions and all(
            s.get("type") != emu.ORDER_TYPE_SELL or "position" in s for s in emu.sent
        )
        async with db.connect() as conn:
            held = await conn.scalar(
                select(positions.c.quantity).where(positions.c.account_id == h.env.account_id)
            )
        assert held == 0
        assert await h.reconcile() == ()  # cash and holdings match the broker exactly


async def test_a_stop_out_with_swap_is_booked_and_reconciled(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        entry = await buy_gold(h)
        emu.rollover("XAUUSD", "-78.56")  # one night of swap on 0.01 lot
        emu.move("XAUUSD", "3999.00", "3999.30")  # the stop on the position is touched
        for _ in range(3):
            await h.env.svc.execution.run_cycle()
        async with db.connect() as conn:
            stop = (
                await conn.execute(
                    select(orders).where(
                        orders.c.intent_id == entry.intent_id, orders.c.purpose == "STOP_LOSS"
                    )
                )
            ).one()
            sold = (await conn.execute(select(fills).where(fills.c.order_id == stop.id))).one()
        assert stop.status == "FILLED" and sold.quantity == D("1")
        assert sold.fee == D("0.03") + D("0.7856")  # commission and the night's swap
        assert await h.reconcile() == ()


@pytest.mark.parametrize("mode", ["REAL"])
async def test_a_real_account_is_never_traded(
    settings: Settings, db: Database, tmp_path: Path, mode: str
) -> None:
    from tests.mt5_emulator import ACCOUNT_TRADE_MODE_REAL

    emu = MT5Emulator(trade_mode=ACCOUNT_TRADE_MODE_REAL)
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        steps = await run_contract(h.adapter)
    assert steps and not steps[-1].passed and not emu.sent


async def test_the_check_never_leaves_its_order_resting(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    from cosmic.trading.brokers.base import BrokerUnavailableError

    emu = MT5Emulator()
    h = await build(settings, db, emu)

    async def broken(*_a: object, **_k: object) -> list[object]:
        raise BrokerUnavailableError("listing failed (test)")

    h.adapter.list_open_orders = broken  # type: ignore[method-assign]
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        steps = await run_contract(h.adapter)
    assert not steps[-1].passed and not emu.orders  # failed, and the order was removed


async def test_the_entry_is_protected_before_its_stop_order_exists(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """HFM holds the stop from the fill. If it closes the position before the protection step
    has recorded the stop order, that order is booked as filled and the books stay exact."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        entry = await propose_gold(h)
        # Only the entry is sent: filled at HFM with its stop, before any stop order exists.
        await h.env.svc.execution.execute_intent(entry.intent_id)
        (pos,) = emu.positions.values()
        assert pos.sl == D("4000.00")
        async with db.connect() as conn:
            purposes = [
                o.purpose
                for o in await conn.execute(
                    select(orders).where(orders.c.intent_id == entry.intent_id)
                )
            ]
        assert purposes == ["ENTRY"]
        assert await h.reconcile() == ()  # the entry's own stop is not an unknown order
        emu.move("XAUUSD", "3999.00", "3999.30")  # HFM's stop closes the position
        for _ in range(4):
            await h.env.svc.execution.run_cycle()
        async with db.connect() as conn:
            stop = (
                await conn.execute(
                    select(orders).where(
                        orders.c.intent_id == entry.intent_id, orders.c.purpose == "STOP_LOSS"
                    )
                )
            ).one()
            sold = (await conn.execute(select(fills).where(fills.c.order_id == stop.id))).one()
            held = await conn.scalar(
                select(positions.c.quantity).where(positions.c.account_id == h.env.account_id)
            )
        assert stop.status == "FILLED" and sold.quantity == D("1") and sold.price == D("3999.00")
        assert held == 0 and not emu.positions
        assert await h.reconcile() == ()


async def test_an_old_bridge_is_refused_an_entry_with_a_stop(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bridge that cannot send the stop with the buy gets no entry at all: nothing is sent."""
    import bridge.cosmic_mt5_bridge as program

    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        monkeypatch.setattr(program, "VERSION", "1")
        for _ in range(1000):  # until the old version's report has arrived (up to 20 s)
            async with db.connect() as conn:
                snap = await conn.scalar(select(bridge_state.c.snapshot))
            if snap["version"] == "1":
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("the old bridge version never reported")
        sent = len(emu.sent)
        entry = await propose_gold(h)
        await h.env.svc.execution.run_cycle()
        async with db.connect() as conn:
            row = (
                await conn.execute(
                    select(orders).where(
                        orders.c.intent_id == entry.intent_id, orders.c.purpose == "ENTRY"
                    )
                )
            ).one()
        assert row.status == "REJECTED"
        assert len(emu.sent) == sent and not emu.positions  # nothing reached MT5


async def _open_incidents(db: Database) -> dict[str, str]:
    """Open incidents about HFM (other feeds never fetch in these tests)."""
    async with db.connect() as conn:
        rows = await conn.execute(
            select(incidents.c.dedup_key, incidents.c.severity).where(
                incidents.c.status != "RESOLVED"
            )
        )
        return {r.dedup_key: r.severity for r in rows if "hfm" in r.dedup_key}


async def test_a_silent_bridge_is_one_incident_and_never_a_disagreement(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """When the bridge stops, its two sources go stale together and reconciliation cannot reach
    HFM: one bridge incident, and an honest 'not reconciled' one (WARNING while flat, CRITICAL
    with a position) instead of 'our records and the venue disagree'."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
    h.env.clock.advance(seconds=200)  # the bridge has gone quiet
    emu.now_ms += 200_000  # the market itself keeps ticking
    await check_source_freshness(h.ctx)
    assert await h.reconcile() != ()  # not reconciled: the venue cannot be reached
    assert await _open_incidents(db) == {
        "data.bridge_offline:hfm-demo": "WARNING",
        "accounting.venue_unreachable:hfm-demo": "WARNING",
    }
    async with db.connect() as conn:
        lines = await health_lines(conn, h.env.clock.now())
    assert "HFM bridge v2: NOT REPORTING for 3 min: HFM orders wait" in lines
    async with bridge_running(db, h.env.clock, emu, tmp_path):  # back
        for _ in range(250):  # until its heartbeat from now has been stored
            async with db.connect() as conn:
                beat = await conn.scalar(
                    select(sources.c.last_success_at).where(sources.c.key == "hfm_mt5_status")
                )
            if beat == h.env.clock.now():
                break
            await asyncio.sleep(0.02)
        await check_source_freshness(h.ctx)
        assert await h.reconcile() == ()
        assert await _open_incidents(db) == {}
        async with db.connect() as conn:
            lines = await health_lines(conn, h.env.clock.now())
        assert "HFM bridge v2: report 0 s ago (DEMO)" in lines
        await paxg(db, h.env.clock, "4100.10", "4100.40")  # a fresh reference price (R10)
        await buy_gold(h)
    h.env.clock.advance(seconds=200)
    emu.now_ms += 200_000
    await h.reconcile()
    assert (await _open_incidents(db))["accounting.venue_unreachable:hfm-demo"] == "CRITICAL"


async def _status_ingested(db: Database, at: object) -> None:
    for _ in range(1000):  # until the bridge's status from ``at`` has been stored (up to 20 s)
        async with db.connect() as conn:
            done = await conn.scalar(
                select(sources.c.last_success_at).where(sources.c.key == "hfm_mt5_status")
            )
        if done == at:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the bridge's status was not stored")


async def test_a_disconnected_terminal_blocks_new_hfm_trades_until_it_reconnects(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """Directive Phase 11: MT5 loses its own connection to HFM while the bridge keeps
    reporting. HFM's status is no longer OPERATIONAL, so R10 refuses new trades; once the
    terminal reconnects, trading resumes."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        emu.connected = False
        h.env.clock.advance(seconds=31)  # the next status report is due
        emu.now_ms += 31_000
        await _status_ingested(db, h.env.clock.now())
        await paxg(db, h.env.clock, "4100.10", "4100.40")
        cut = await h.env.propose(
            account_alias="hfm-demo",
            instrument_key=XAU,
            quantity=D("1"),
            limit_price=D("4101.00"),
            price_band_low=D("4090.00"),
            price_band_high=D("4110.00"),
            management_plan={
                "stop_loss": {"stop_price": D("4000.00"), "limit_price": D("3990.00")}
            },
            entry_conditions="Buy 1 oz at or below 4,101.",
        )
        assert cut.status == "NON_EXECUTABLE"
        assert any(p.startswith("R10") for p in cut.problems), cut.problems
        emu.connected = True
        h.env.clock.advance(seconds=31)
        emu.now_ms += 31_000
        await _status_ingested(db, h.env.clock.now())
        await paxg(db, h.env.clock, "4100.10", "4100.40")
        back = await propose_gold(h)  # proposes and approves
        assert back.status == "PROPOSED", back.problems


async def test_a_cancel_with_the_bridge_silent_fails_at_once_and_queues_nothing(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """With the bridge silent, a cancel used to wait the adapter's full answer time while it
    held the execution cycle. It now fails at once; the follow-up sends it again later."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
    async with db.connect() as conn:
        coid = next(
            c.payload["client_order_id"]
            for c in await conn.execute(
                select(bridge_commands).where(bridge_commands.c.kind == "SUBMIT")
            )
        )
        before = await conn.scalar(select(func.count()).select_from(bridge_commands))
    h.env.clock.advance(seconds=200)  # the bridge has gone quiet
    started = asyncio.get_running_loop().time()
    with pytest.raises(BrokerUnavailableError, match="last reported 200 s ago"):
        await h.adapter.cancel_order(h.env.account_id, coid)
    assert asyncio.get_running_loop().time() - started < 2  # the adapter would wait 10 s
    async with db.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(bridge_commands)) == before


async def _intent_orders(db: Database, intent_id: Any) -> list[Any]:
    async with db.connect() as conn:
        return list(
            await conn.execute(
                select(orders).where(orders.c.intent_id == intent_id).order_by(orders.c.created_at)
            )
        )


async def test_an_old_stops_cancel_never_clears_the_stop_a_newer_one_holds(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """HFM keeps one stop-loss per position: every stop order of an entry sets the same stop,
    and a cancel clears it for all of them. Half the entry fills and gets SL1; SL1's cancel is
    lost; the rest fills and gets SL2. SL1's cancel is then not sent again, because it would
    clear the stop-loss SL2 holds. Found by review on 2026-10-17."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    ex = h.env.svc.execution
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        r = await h.env.propose(
            account_alias="hfm-demo",
            instrument_key=XAU,
            quantity=D("2"),
            limit_price=D("4095.00"),
            price_band_low=D("4090.00"),
            price_band_high=D("4110.00"),
            management_plan={
                "stop_loss": {"stop_price": D("4000.00"), "limit_price": D("3990.00")}
            },
            entry_conditions="Buy 2 oz at or below 4,095.",
        )
        assert r.status == "PROPOSED", r.problems
        await h.env.approve(r.intent_id)
        await ex.run_cycle()  # the BUY_LIMIT rests, carrying its stop-loss
        ((ticket, pending),) = emu.orders.items()
        sl = D(repr(pending["sl"]))
        emu._buy("XAUUSD", D("0.01"), D("4095.00"), pending["magic"], pending["comment"], None, sl)
        pending["volume_current"] = 0.01  # half fills, the rest keeps resting
        for _ in range(3):
            await ex.run_cycle()
        sl1 = next(o for o in await _intent_orders(db, r.intent_id) if o.purpose == "STOP_LOSS")
        assert sl1.status == "ACKNOWLEDGED"
        emu.faults = ["none"]  # the terminal answers nothing: the cancel never happened
        await ex.request_cancel(sl1.id, reason="approved plan: take-profit target reached")
        del emu.orders[ticket]  # the rest fills
        emu._buy(
            "XAUUSD", D("0.01"), D("4095.00"), pending["magic"], pending["comment"], ticket, sl
        )
        for _ in range(250):  # until that fill is imported and SL2 protects it
            await ex.run_cycle()
            if sum(o.purpose == "STOP_LOSS" for o in await _intent_orders(db, r.intent_id)) > 1:
                break
            await asyncio.sleep(0.02)
        for _ in range(3):  # the follow-up would resend SL1's cancel in each of these
            h.env.clock.advance(seconds=61)
            await _status_ingested(db, h.env.clock.now())  # a fresh bridge: nothing fails fast
            await ex.run_cycle()
        stops = {
            o.client_order_id.rsplit("-", 1)[1]: o.status
            for o in await _intent_orders(db, r.intent_id)
            if o.purpose == "STOP_LOSS"
        }
        async with db.connect() as conn:
            sl1_cancels = await conn.scalar(
                select(func.count())
                .select_from(bridge_commands)
                .where(
                    bridge_commands.c.kind == "CANCEL",
                    bridge_commands.c.payload["client_order_id"].astext == sl1.client_order_id,
                )
            )
            ended = await conn.scalar(
                select(func.count())
                .select_from(incidents)
                .where(
                    incidents.c.status != "RESOLVED",
                    incidents.c.dedup_key.like("trading.protection_ended_at_venue:%"),
                )
            )
    assert sl1_cancels == 1  # the lost request only: never sent again
    assert [p.sl for p in emu.positions.values()] == [sl, sl]  # still protected at MT5
    assert stops == {"SL1": "CANCEL_PENDING", "SL2": "ACKNOWLEDGED"} and ended == 0
