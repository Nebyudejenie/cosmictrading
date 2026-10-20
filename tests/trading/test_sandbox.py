"""Test-network (SANDBOX) accounts end to end against the Binance emulator (ADR-026).

The emulator follows the official Spot API documentation (``tests/binance_emulator.py``):
signed requests, filters, and commission charged in the asset received. The whole path runs
on it here: the contract-run gate, opening from venue balances, proposal → approval → Binance
order → fill with a base-asset fee → protective stop on the sellable quantity → owner exit →
dust → reconciliation, all labelled SIMULATED.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal as D
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from pydantic import SecretStr
from sqlalchemy import func, select

from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import incidents, jobs
from cosmic.db.schema_execution import fills, orders, post_trade_reviews, trade_intents
from cosmic.db.schema_market import (
    fetch_runs,
    instruments,
    quotes,
    sources,
    venue_status,
    venues,
)
from cosmic.db.schema_trading import accounts, broker_contract_runs, positions
from cosmic.market import data as md
from cosmic.market.jobs import build_ingest_service
from cosmic.sources.ingest import ensure_registry
from cosmic.trading import authority, ledger
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers import binance_testnet
from cosmic.trading.brokers.binance_check import CONTRACT_VERSION, Step
from cosmic.trading.exits import propose_close
from cosmic.trading.jobs import open_sandbox
from cosmic.trading.modes import Mode
from cosmic.trading.policy import active_policy, change_policy, ensure_default_policy
from cosmic.trading.sandbox import key_has_passed, record_contract_run
from cosmic.trading.services import TradingServices
from cosmic.worker.context import WorkerContext
from tests.binance_emulator import BinanceEmulator
from tests.helpers import make_user, principal_for
from tests.market_mock import MarketMock
from tests.trading_env import CAPTURE, Env

BTC = "SPOT:BTC/USDT@BINANCE_TESTNET"
REAL = "SPOT:BTC/USDT@BINANCE"
ALIAS = "binance-test"


@dataclass
class Sandbox:
    env: Env
    emu: BinanceEmulator
    ctx: WorkerContext
    broker: binance_testnet.SandboxBroker

    async def propose(self, **kw: object) -> uuid.UUID:
        r = await self.env.propose(account_alias=ALIAS, instrument_key=BTC, **kw)
        assert r.status == "PROPOSED", r.problems
        return r.intent_id

    async def reconcile(self) -> tuple[str, ...]:
        result = await self.env.svc.reconciler.reconcile(self.env.account_id, trigger="test")
        return result.mismatches

    async def book(self, book: str, asset: str) -> D:
        async with self.env.db.connect() as conn:
            return await ledger.book_balance(conn, self.env.account_id, book, asset)


def keypair() -> tuple[str, bytes]:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    pub = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return pem, pub


async def set_market(
    db: Database, clock: FrozenClock, emu: BinanceEmulator, bid: str, ask: str
) -> None:
    """Fresh BTC/USDT quotes on Binance and on its test network, and the emulator's book."""
    now = clock.now()
    async with db.transaction() as conn:
        for key, source in ((BTC, "binance_testnet_public"), (REAL, "binance_public")):
            inst = await conn.scalar(
                select(instruments.c.id).where(instruments.c.canonical_key == key)
            )
            src = await conn.scalar(select(sources.c.id).where(sources.c.key == source))
            run_id = uuid7()
            await conn.execute(
                fetch_runs.insert().values(
                    id=run_id,
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
                    bid=D(bid),
                    ask=D(ask),
                    last=D(bid),
                    source_ts=now,
                    ingested_at=now,
                    fetch_run_id=run_id,
                )
            )
        await conn.execute(
            venue_status.insert().values(
                id=uuid7(),
                venue_id=await conn.scalar(
                    select(venues.c.id).where(venues.c.key == "BINANCE_TESTNET")
                ),
                source_id=await conn.scalar(
                    select(sources.c.id).where(sources.c.key == "binance_testnet_status")
                ),
                status="OPERATIONAL",
                indicator="ping",
                description="test",
                source_ts=now,
                ingested_at=now,
            )
        )
    emu.move("BTCUSDT", bid, ask)


async def build(
    settings: Settings,
    db: Database,
    *,
    passed: bool = True,
    capital: str = "5000",
    fee_asset: str | None = None,
    open_account: bool = True,
) -> Sandbox:
    clock = FrozenClock(CAPTURE)
    pem, pub = keypair()
    emu = BinanceEmulator(
        hmac_secret=None,
        ed25519_public_pem=pub,
        server_ms=int(CAPTURE.timestamp() * 1000),
        fee_asset=fee_asset,
    )
    if fee_asset is not None:
        emu.balances[fee_asset] = [D("1"), D(0)]
    keyed = settings.model_copy(
        update={
            "binance_testnet_api_key": SecretStr(emu.api_key),
            "binance_testnet_private_key": SecretStr(pem),
        }
    )
    broker = binance_testnet.build(keyed, clock, httpx.AsyncClient(transport=emu.transport()))
    assert broker is not None
    ctx = WorkerContext(settings=keyed, db=db, clock=clock, worker_id="test")
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    ingest = build_ingest_service(ctx, transport=MarketMock().transport())
    for key in ("binance_testnet_public", "binance_public"):
        await ingest.sync_instruments(key)
    await ingest.ingest_venue_status("binance_testnet_status")
    owner = await make_user(db, clock)
    svc = TradingServices.build(db, clock, keyed, sandbox=broker)
    ctx.services["trading"] = svc
    p = principal_for(owner, clock)
    async with db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=p,
            target=Mode.PAPER_TRADING,
            reason="test-network trading for tests",
            now=clock.now(),
            settings=keyed,
        )
        _, policy = await active_policy(conn)
        params = policy.model_dump()
        params.update(
            allowed_instruments=(BTC,),
            allowed_venues=("BINANCE_TESTNET",),
            allowed_quote_currencies=("USDT",),
        )
        await change_policy(
            conn,
            principal=p,
            new_params=params,
            reason="allow the Binance test network for tests",
            now=clock.now(),
            settings=keyed,
        )
    await set_market(db, clock, emu, "82990.00", "83000.00")
    if passed:
        async with db.transaction() as conn:
            await record_contract_run(
                conn,
                steps=[Step("venue reachable (ping)", True)],
                key_fingerprint=broker.key_fingerprint,
                code_version=CONTRACT_VERSION,
                started_at=clock.now(),
                finished_at=clock.now(),
                actor=Actor.system("test"),
            )
    account_id = None
    if open_account:
        account_id = await open_sandbox(
            ctx, {"alias": ALIAS, "managed_capital": capital, "user_id": str(owner.user_id)}
        )
    env = Env(keyed, db, clock, svc, owner, account_id or uuid.UUID(int=0))
    return Sandbox(env, emu, ctx, broker)


async def count(db: Database, table: Any, *where: Any) -> int:
    async with db.connect() as conn:
        return int(await conn.scalar(select(func.count()).select_from(table).where(*where)) or 0)


async def test_contract_runs_are_evidence_and_identify_the_key(
    settings: Settings, db: Database
) -> None:
    sbx = await build(settings, db, passed=False, open_account=False)
    fp = sbx.broker.key_fingerprint
    assert len(fp) == 16 and sbx.emu.api_key not in fp
    async with db.transaction() as conn:
        _, ok = await record_contract_run(
            conn,
            steps=[Step("venue accepted the request", False, "not accepted: -2015")],
            key_fingerprint=fp,
            code_version=CONTRACT_VERSION,
            started_at=sbx.env.clock.now(),
            finished_at=sbx.env.clock.now(),
            actor=Actor.system("test"),
        )
        assert ok is False and not await key_has_passed(conn, fp)
    with pytest.raises(Exception, match=r"permission denied|immutable|not allowed"):
        async with db.transaction() as conn:
            await conn.execute(broker_contract_runs.update().values(status="PASSED"))


async def test_no_account_without_a_passed_run_for_the_workers_key(
    settings: Settings, db: Database
) -> None:
    sbx = await build(settings, db, passed=False, open_account=False)
    async with db.transaction() as conn:  # a pass for another key does not count
        await record_contract_run(
            conn,
            steps=[Step("venue reachable (ping)", True)],
            key_fingerprint="0123456789abcdef",
            code_version=CONTRACT_VERSION,
            started_at=sbx.env.clock.now(),
            finished_at=sbx.env.clock.now(),
            actor=Actor.system("test"),
        )
    payload = {"alias": ALIAS, "managed_capital": "5000"}
    assert await open_sandbox(sbx.ctx, payload) is None
    assert await count(db, accounts) == 0
    assert await count(
        db, incidents, incidents.c.dedup_key == f"trading.sandbox_not_opened:{ALIAS}"
    )
    assert "POST /v3/order" not in sbx.emu.requests


async def test_a_refused_opening_names_the_venue_it_was_for(
    settings: Settings, db: Database
) -> None:
    """DIR-02: the alert names the account with its own environment; an HFM demo account is
    never described as the Binance test network."""
    sbx = await build(settings, db, open_account=False)
    hfm = {"alias": "hfm-demo", "managed_capital": "1000", "adapter": "hfm_mt5_demo"}
    assert await open_sandbox(sbx.ctx, hfm) is None  # no HFM bridge on this worker
    assert await open_sandbox(sbx.ctx, {"alias": ALIAS, "managed_capital": "x"}) is None
    async with db.connect() as conn:
        rows = {
            r.dedup_key: r
            for r in await conn.execute(
                select(incidents.c.dedup_key, incidents.c.title, incidents.c.detail)
            )
        }
    demo = rows["trading.sandbox_not_opened:hfm-demo"]
    assert demo.title == "hfm-demo [SIMULATED · DEMO]: account was not opened"
    assert "HFM MetaTrader 5 demo account" in demo.detail["alert"]["what_happened"]
    assert "Binance" not in demo.detail["alert"]["what_happened"]
    testnet = rows[f"trading.sandbox_not_opened:{ALIAS}"]
    assert testnet.title == f"{ALIAS} [SIMULATED · TESTNET]: account was not opened"
    assert "Binance Spot Test Network" in testnet.detail["alert"]["what_happened"]


async def test_opening_imports_balances_and_reconciles(settings: Settings, db: Database) -> None:
    sbx = await build(settings, db)
    async with db.connect() as conn:
        acct = await get_account(conn, account_id=sbx.env.account_id)
    assert (acct.kind, acct.venue_key, acct.broker_adapter, acct.base_currency) == (
        "SANDBOX",
        "BINANCE_TESTNET",
        "binance_spot_testnet",
        "USDT",
    )
    assert acct.reconciliation_status == "OK"  # reconciled at once against the venue
    assert await sbx.book("CASH", "USDT") == D("5000")  # the capital the owner chose
    assert await sbx.book("UNMANAGED", "USDT") == D("5000")  # the rest of the venue's USDT
    assert await sbx.book("UNMANAGED", "BTC") == D("1")
    assert await sbx.book("UNMANAGED", "PAXG") == D("2")
    # Equity counts only what the account manages.
    from cosmic.trading.context_builder import account_state

    async with db.connect() as conn:
        state = await account_state(conn, acct, sbx.env.clock.now())
    assert state.equity == D("5000")


async def test_capital_is_bounded_and_one_account_per_key(settings: Settings, db: Database) -> None:
    sbx = await build(settings, db, open_account=False)
    for bad in ("10000.01", "NaN", "Infinity", "-5", "", "x"):
        assert await open_sandbox(sbx.ctx, {"alias": ALIAS, "managed_capital": bad}) is None
    assert await count(db, accounts) == 0
    assert await open_sandbox(sbx.ctx, {"alias": ALIAS, "managed_capital": "1000"}) is not None
    assert await open_sandbox(sbx.ctx, {"alias": "second", "managed_capital": "1000"}) is None
    assert await count(db, accounts) == 1
    async with db.connect() as conn:
        titles = [r.title for r in await conn.execute(select(incidents.c.title))]
    assert any("second" in t for t in titles)


async def test_full_trade_with_fees_in_the_received_asset(settings: Settings, db: Database) -> None:
    sbx = await build(settings, db)
    env = sbx.env
    entry = await sbx.propose()
    await env.approve(entry)
    await env.cycle()  # BUY 0.004 sent to the test network; fills at the ask
    await env.cycle()  # fill imported; protective stop placed
    async with db.connect() as conn:
        rows = list(await conn.execute(select(orders).where(orders.c.intent_id == entry)))
        pos = (
            await conn.execute(select(positions).where(positions.c.account_id == env.account_id))
        ).one()
        fill = (await conn.execute(select(fills))).one()
    by = {o.purpose: o for o in rows}
    assert by["ENTRY"].status == "FILLED" and by["ENTRY"].filled_qty == D("0.004")
    assert all(o.is_simulated for o in rows) and fill.is_simulated
    assert (fill.fee, fill.fee_currency) == (D("0.000004"), "BTC")
    assert pos.quantity == D("0.003996")  # received net of the commission
    # cost basis = quantity × price, spread over what was received (NUMERIC(38,18))
    assert pos.avg_cost == (D("0.004") * D("83000") / D("0.003996")).quantize(D("1e-18"))
    stop = by["STOP_LOSS"]
    assert stop.status == "ACKNOWLEDGED" and stop.quantity == D("0.00399")  # whole lots only
    assert await sbx.book("POSITION", "BTC") == D("0.003996")
    assert await sbx.book("CASH", "USDT") == D("5000") - D("332")
    assert await sbx.reconcile() == ()

    # The owner closes the position: the stop is replaced by an IOC sale of whole lots.
    async with db.transaction() as conn:
        acct = await get_account(conn, account_id=env.account_id)
        inst = await md.instrument_by_key(conn, BTC)
        exit_ = await propose_close(
            conn,
            account=acct,
            inst=inst,
            reason="test-network round trip",
            actor=Actor.user(env.owner.user_id),
            now=env.clock.now(),
        )
    assert exit_.status == "PROPOSED", exit_.problems
    await env.approve(exit_.intent_id)
    await env.cycle()
    await env.cycle()
    async with db.connect() as conn:
        rows = list(await conn.execute(select(orders).where(orders.c.intent_id == entry)))
        exit_row = (
            await conn.execute(select(trade_intents).where(trade_intents.c.id == exit_.intent_id))
        ).one()
        left = await conn.scalar(
            select(positions.c.quantity).where(positions.c.account_id == env.account_id)
        )
        reviews = await conn.scalar(select(func.count()).select_from(post_trade_reviews))
    by = {o.purpose: o for o in rows}
    assert by["STOP_LOSS"].status == "CANCELLED"
    assert by["EXIT"].status == "FILLED" and by["EXIT"].filled_qty == D("0.00399")
    assert exit_row.status == "EXECUTED"
    assert left == D("0.000006")  # dust below one lot (0.00001) stays on the books
    assert reviews == 1  # the trade counts as closed
    await env.cycle()
    async with db.connect() as conn:
        stops = await conn.scalar(
            select(func.count())
            .select_from(orders)
            .where(orders.c.intent_id == entry, orders.c.purpose == "STOP_LOSS")
        )
    assert stops == 1  # nothing tries to protect dust
    assert await sbx.reconcile() == ()
    with pytest.raises(Exception, match="smallest tradable step"):
        async with db.transaction() as conn:
            await propose_close(
                conn,
                account=acct,
                inst=inst,
                reason="sell the dust",
                actor=Actor.user(env.owner.user_id),
                now=env.clock.now(),
            )


async def test_fees_in_another_asset_are_booked_and_reported(
    settings: Settings, db: Database
) -> None:
    sbx = await build(settings, db, fee_asset="BNB")
    env = sbx.env
    entry = await sbx.propose()
    await env.approve(entry)
    await env.cycle(2)
    async with db.connect() as conn:
        pos = await conn.scalar(
            select(positions.c.quantity).where(positions.c.account_id == env.account_id)
        )
        found = await conn.scalar(
            select(func.count())
            .select_from(incidents)
            .where(incidents.c.dedup_key == f"accounting.foreign_fee:{env.account_id}:BNB")
        )
    assert pos == D("0.004")  # nothing taken from the BTC received
    assert found == 1
    assert await sbx.book("FEES", "BNB") == D("0.00001")
    assert await sbx.book("UNMANAGED", "BNB") == D("-0.00001")
    assert await sbx.reconcile() == ()  # BNB is not a tracked asset


async def test_the_web_and_bot_hand_test_network_work_to_the_worker(
    settings: Settings, db: Database
) -> None:
    sbx = await build(settings, db)
    web = TradingServices.build(db, sbx.env.clock, settings)  # holds no testnet keys
    async with db.connect() as conn:
        acct = await get_account(conn, account_id=sbx.env.account_id)
    assert not web.brokers.serves(acct) and sbx.env.svc.brokers.serves(acct)
    assert await web.reconcile_all(trigger="test") == []  # skipped, never crashes
    await web.emergency_stop(
        actor=Actor.user(sbx.env.owner.user_id),
        reason="telegram: test stop",
        principal=sbx.env.principal(),
    )
    async with db.connect() as conn:
        mode = (await authority.get_state(conn)).mode
        queued = await conn.scalar(
            select(func.count())
            .select_from(jobs)
            .where(jobs.c.kind == "trading.emergency_followup", jobs.c.status == "QUEUED")
        )
    assert mode is Mode.EMERGENCY_STOP and queued == 1


async def test_the_key_must_be_an_ed25519_private_key(settings: Settings) -> None:
    clock = FrozenClock(CAPTURE)
    assert binance_testnet.build(settings, clock) is None  # not configured
    bad = settings.model_copy(
        update={
            "binance_testnet_api_key": SecretStr("k" * 64),
            "binance_testnet_private_key": SecretStr("not a key"),
        }
    )
    assert binance_testnet.build(bad, clock) is None  # unusable: SANDBOX fails closed
    assert set(binance_testnet.testnet_symbols()) == {BTC, "SPOT:PAXG/USDT@BINANCE_TESTNET"}
