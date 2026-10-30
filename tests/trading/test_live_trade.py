"""A real-money trade end to end, on the emulator (ADR-034, S40c-2). Every lock is opened for this
test only (a patched ``LIVE_ADAPTERS``, gates reported PASSED, the emulator as Binance). The
daily auto-trader asks; its proposal is priced by the venue's book and lasts through the
owner's day; the owner approves with a code; the order goes out on a fresh book; ending the
activation ends the auto-trader and the live mode.

It runs in real time: the database bounds the application's clock by its own (migration 0016),
so a test clock in the past would make its guards refuse once real time passes the activation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_execution import orders, trade_intents, trading_mandates
from cosmic.db.schema_market import fetch_runs, instruments, quotes, sources
from cosmic.db.schema_trading import risk_check_runs
from cosmic.market.jobs import build_ingest_service
from cosmic.research.library import auto
from cosmic.trading import authority, live, mandates
from cosmic.trading.brokers import binance_live
from cosmic.trading.brokers.binance_live import LiveBroker
from cosmic.trading.brokers.binance_spot import BinanceSpotAdapter, Ed25519Signer
from cosmic.trading.jobs import check_live_key, open_live
from cosmic.trading.modes import Mode
from cosmic.trading.policy import active_policy, change_policy
from cosmic.worker.context import WorkerContext
from tests.binance_emulator import BinanceEmulator
from tests.market_mock import MarketMock
from tests.trading.test_live_locks import _contract_run, _open_locks, _open_settings
from tests.trading.test_sandbox import keypair
from tests.trading_env import Env, build_env

D = Decimal
PAXG = "SPOT:PAXG/USDT@BINANCE"
GOOD = dict.fromkeys(live.KEY_MUST_ALLOW, True) | dict.fromkeys(live.KEY_MUST_REFUSE, False)


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def _daily_gold(env: Env, rows: list[tuple[float, float, float, float]]) -> None:
    """Completed daily PAXG bars on Binance, the last one opened yesterday 00:00 UTC."""
    last_open = mandates.day_start(env.clock.now()) - timedelta(days=1)
    async with env.db.transaction() as conn:
        inst = await conn.scalar(
            select(instruments.c.id).where(instruments.c.canonical_key == PAXG)
        )
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == "binance_public"))
        run, now = uuid7(), env.clock.now()
        await conn.execute(
            fetch_runs.insert().values(
                id=run,
                source_id=src,
                kind="test_bars",
                started_at=now,
                finished_at=now,
                status="SUCCESS",
            )
        )
        first = last_open - timedelta(days=len(rows) - 1)
        params: list[dict[str, Any]] = [
            {
                "i": inst,
                "s": src,
                "t": first + timedelta(days=k),
                "o": D(f"{o:.2f}"),
                "h": D(f"{h:.2f}"),
                "l": D(f"{lo:.2f}"),
                "c": D(f"{c:.2f}"),
                "r": run,
                "n": now,
            }
            for k, (o, h, lo, c) in enumerate(rows)
        ]
        await conn.execute(
            text(
                "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, low, "
                "close, volume, is_complete, fetch_run_id, ingested_at, updated_at) "
                "VALUES (:i, :s, '1d', :t, :o, :h, :l, :c, 1, true, :r, :n, :n)"
            ),
            params,
        )


async def _gold_quotes(env: Env) -> None:
    """Fresh stored gold quotes from Binance and its test network, as the worker polls them:
    R10's cross-venue check compares them (the trade itself is priced by the venue's book)."""
    now = env.clock.now()
    async with env.db.transaction() as conn:
        for key, source in (
            (PAXG, "binance_public"),
            (f"{PAXG}_TESTNET", "binance_testnet_public"),
        ):
            inst = await conn.scalar(
                select(instruments.c.id).where(instruments.c.canonical_key == key)
            )
            src = await conn.scalar(select(sources.c.id).where(sources.c.key == source))
            run = uuid7()
            await conn.execute(
                fetch_runs.insert().values(
                    id=run,
                    source_id=src,
                    kind="test_quote",
                    started_at=now,
                    finished_at=now,
                    status="SUCCESS",
                )
            )
            await conn.execute(
                quotes.insert().values(
                    id=uuid7(),
                    instrument_id=inst,
                    source_id=src,
                    bid=D("4099.90"),
                    ask=D("4100.40"),
                    last=D("4099.90"),
                    source_ts=now,
                    ingested_at=now,
                    fetch_run_id=run,
                )
            )


@dataclass
class Asked:
    """A real-money proposal the auto-trader just asked the owner about."""

    account_id: uuid.UUID
    activation: live.Activation
    mandate_id: uuid.UUID
    emu: BinanceEmulator
    intent: Any


async def _asked(env: Env, monkeypatch: pytest.MonkeyPatch) -> Asked:
    """Every lock opened for the test only, the account opened and activated, the auto-trader
    asking on daily gold, and its first proposal checked."""
    env.clock.set(datetime.now(UTC).replace(microsecond=0))
    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="test")
    ctx.services["trading"] = env.svc
    ingest = build_ingest_service(ctx, transport=MarketMock().transport())
    for key in ("binance_public", "binance_testnet_public"):
        await ingest.sync_instruments(key)
    await ingest.ingest_venue_status("binance_status")
    opened = _open_settings(env)
    async with env.db.transaction() as conn:  # the owner allows gold on Binance, with its fee
        _, policy = await active_policy(conn)
        params = policy.model_dump()
        params.update(
            allowed_instruments=(*params["allowed_instruments"], PAXG),
            allowed_venues=(*params["allowed_venues"], "BINANCE"),
            allowed_quote_currencies=(*params["allowed_quote_currencies"], "USDT"),
            venue_fees=[{"venue": "BINANCE", "bps": "10"}],
        )
        await change_policy(
            conn,
            principal=env.principal(),
            new_params=params,
            reason="gold on Binance for the real-money test",
            now=env.clock.now(),
            settings=opened,
        )
    # Every lock opened for this test only; the emulator plays Binance.
    _open_locks(monkeypatch)
    await _contract_run(env)
    pem, pub = keypair()
    emu = BinanceEmulator(hmac_secret=None, ed25519_public_pem=pub)
    emu.server_ms = int(env.clock.now().timestamp() * 1000)
    emu.book["PAXGUSDT"] = (D("4100.00"), D("4100.50"))
    emu.key_permissions = dict(GOOD)
    adapter = BinanceSpotAdapter.live_link(
        httpx.AsyncClient(transport=emu.transport()),
        api_key=emu.api_key,
        signer=Ed25519Signer(pem.encode()),
        symbols=binance_live.live_symbols(),
        clock=env.clock,
    )
    env.svc.brokers.live = LiveBroker(adapter, live.key_fingerprint(emu.api_key))
    check = await check_live_key(ctx)
    assert check is not None and check.passed
    account_id = await open_live(
        ctx, {"alias": "real-binance", "managed_capital": "100", "user_id": str(env.owner.user_id)}
    )
    assert account_id is not None
    async with env.db.transaction() as conn:
        made = await live.activate(
            conn,
            principal=env.principal(),
            settings=opened,
            account_id=account_id,
            cap=D(100),
            days=30,
            reason="the rehearsal passed; start with $100",
            now=env.clock.now(),
        )
        await authority.change_mode(
            conn,
            principal=env.principal(),
            target=Mode.APPROVAL_REQUIRED,
            reason="every lock open; one trade at a time",
            now=env.clock.now(),
            settings=opened,
        )
        terms = mandates.terms_from_money(
            account_alias="real-binance",
            money=D(100),
            care="BOLD",
            mode="ASK",
            instruments=(PAXG,),
            strategy_key="turtle_20_10",
            timeframe=mandates.DAILY,
        )
        with pytest.raises(Exception, match="may only ask before each trade"):
            await mandates.grant(
                conn,
                principal=env.principal(),
                terms=terms.model_copy(update={"mode": "AUTO"}),
                now=env.clock.now(),
                settings=opened,
            )
    async with env.db.transaction() as conn:
        mandate_id = await mandates.grant(
            conn, principal=env.principal(), terms=terms, now=env.clock.now(), settings=opened
        )
        expires = await conn.scalar(
            select(trading_mandates.c.expires_at).where(trading_mandates.c.id == mandate_id)
        )
    assert expires <= made.expires_at  # it ends when real money does

    # A breakout on yesterday's daily bar: the auto-trader asks, priced by the venue's book.
    base = 3900.0
    quiet = [(base, base + 20, base - 20, base + (10 if i % 2 else -10)) for i in range(449)]
    await _daily_gold(env, [*quiet, (base, base + 220, base - 20, base + 200)])
    await _gold_quotes(env)
    assert await auto.run(env.db, env.clock) == []  # without the venue's book: it waits
    done = await auto.run(env.db, env.clock, env.svc.brokers.venue_quote)
    assert done, done
    async with env.db.connect() as conn:
        intent = (
            await conn.execute(
                select(trade_intents).where(trade_intents.c.account_id == account_id)
            )
        ).one()
        checked = (
            await conn.execute(
                select(risk_check_runs.c.context).where(
                    risk_check_runs.c.intent_id == intent.id,
                    risk_check_runs.c.stage == "PRE_APPROVAL",
                )
            )
        ).scalar_one()
        asked = await conn.scalar(
            select(notifications.c.kind).where(
                notifications.c.kind == "approval_requested",
                notifications.c.dedup_key.like(f"approval:{intent.id}%"),
            )
        )
    assert intent.status == "PROPOSED", intent.status_reason
    assert asked == "approval_requested"
    assert intent.intent_expires_at - intent.created_at == mandates.LIVE_ASK_TTL  # the owner's day
    assert checked["market"]["quote_label"] == "LIVE" and D(checked["market"]["ask"]) == D(
        "4100.50"
    )

    return Asked(account_id, made, mandate_id, emu, intent)


async def test_a_real_money_trade_asks_first_is_priced_by_the_venue_and_ends_with_its_activation(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await _asked(env, monkeypatch)
    account_id, made, mandate_id, emu, intent = (
        a.account_id,
        a.activation,
        a.mandate_id,
        a.emu,
        a.intent,
    )
    emu.book["PAXGUSDT"] = (D("4102.00"), D("4102.40"))  # the market moves, within the band
    # The owner approves with a code; the order goes out on a fresh book, as real money.
    await env.approve(intent.id)
    await env.svc.execution.run_cycle()
    async with env.db.connect() as conn:
        entry = (
            await conn.execute(
                select(orders).where(orders.c.account_id == account_id, orders.c.purpose == "ENTRY")
            )
        ).one()
        sent = (
            await conn.execute(
                select(risk_check_runs.c.context).where(
                    risk_check_runs.c.intent_id == intent.id,
                    risk_check_runs.c.stage == "PRE_SUBMISSION",
                )
            )
        ).scalar_one()
    assert entry.is_simulated is False and entry.side == "BUY"
    assert sent["market"]["quote_label"] == "LIVE"
    assert D(sent["market"]["ask"]) == D("4102.40")  # the book read as it was sent
    assert any(o.client_order_id == entry.client_order_id for o in emu.orders.values())
    assert entry.quantity * entry.limit_price <= made.cap

    # Ending real money ends its auto-trader and the live mode.
    async with env.db.transaction() as conn:
        await live.revoke_by_owner(
            conn,
            principal=env.principal(),
            activation_id=made.id,
            reason="the test is over",
            now=env.clock.now(),
        )
    async with env.db.connect() as conn:
        status = await conn.scalar(
            select(trading_mandates.c.status).where(trading_mandates.c.id == mandate_id)
        )
        mode = (await authority.get_state(conn)).mode
    assert (status, mode) == ("REVOKED", Mode.PAPER_TRADING)


async def test_stopping_the_real_money_auto_trader_ends_its_waiting_proposal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await _asked(env, monkeypatch)
    async with env.db.transaction() as conn:
        await mandates.revoke(
            conn,
            principal=env.principal(),
            mandate_id=a.mandate_id,
            reason="stopped by the owner",
            now=env.clock.now(),
        )
    async with env.db.connect() as conn:
        status, why = (
            await conn.execute(
                select(trade_intents.c.status, trade_intents.c.status_reason).where(
                    trade_intents.c.id == a.intent.id
                )
            )
        ).one()
    assert status == "BLOCKED" and "the real-money auto-trader stopped" in why
    with pytest.raises(Exception):  # noqa: B017 - whichever refusal: it is not approvable
        await env.approve(a.intent.id)
    await env.svc.execution.run_cycle()
    async with env.db.connect() as conn:
        assert (
            await conn.scalar(select(orders.c.id).where(orders.c.account_id == a.account_id))
            is None
        )
    assert not a.emu.orders


async def test_a_real_money_proposal_approved_after_the_price_left_its_band_ends(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approving late on a day the price moved more than its band: refused when sent, and the
    proposal ends (BLOCKED) rather than waiting for a re-approval no one can price."""
    a = await _asked(env, monkeypatch)
    a.emu.book["PAXGUSDT"] = (D("4160.00"), D("4160.40"))  # 1.5 % higher by the owner's lunch
    await env.approve(a.intent.id)
    await env.svc.execution.run_cycle()
    async with env.db.connect() as conn:
        status, why = (
            await conn.execute(
                select(trade_intents.c.status, trade_intents.c.status_reason).where(
                    trade_intents.c.id == a.intent.id
                )
            )
        ).one()
        sent = await conn.scalar(select(orders.c.id).where(orders.c.account_id == a.account_id))
    assert status == "BLOCKED" and "band" in why, why
    assert sent is None and not a.emu.orders
