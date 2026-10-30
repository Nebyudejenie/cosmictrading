"""The Binance real-money link (ADR-034, S40b): built and kept off. The same adapter code as the
test network, reached only through its own constructor and only while the server-side locks
are open; the key's own permissions read from the real host only; a real-money check priced
only by the venue's book just read. Every venue call goes to the in-process emulator."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.core.labels import DataLabel
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.db.schema_trading import live_key_checks
from cosmic.trading import authority, live
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.brokers import binance_check, binance_live
from cosmic.trading.brokers.base import BrokerUnavailableError, VenueQuote
from cosmic.trading.brokers.binance_live import LiveBroker
from cosmic.trading.brokers.binance_spot import (
    LIVE_BASES,
    TESTNET_BASES,
    BinanceSpotAdapter,
    Ed25519Signer,
)
from cosmic.trading.context_builder import venue_market
from cosmic.trading.jobs import check_live_key
from cosmic.trading.risk.checks import Status, live_price_too_old, r09_price_validity
from cosmic.worker.context import WorkerContext
from tests.binance_emulator import BinanceEmulator
from tests.risk_factory import NOW, POLICY, account, context, market
from tests.trading.test_live_locks import GOOD_KEY
from tests.trading.test_sandbox import keypair
from tests.trading_env import Env, build_env

D = Decimal
PAXG = "SPOT:PAXG/USDT@BINANCE"


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


def _emulator() -> tuple[BinanceEmulator, str]:
    pem, pub = keypair()
    return BinanceEmulator(hmac_secret=None, ed25519_public_pem=pub), pem


def _adapter(emu: BinanceEmulator, pem: str, *, live_link: bool = True) -> BinanceSpotAdapter:
    client = httpx.AsyncClient(transport=emu.transport())
    clock = FrozenClock(datetime.fromtimestamp(emu.server_ms / 1000, tz=UTC))
    kw: dict[str, Any] = {
        "api_key": emu.api_key,
        "signer": Ed25519Signer(pem.encode()),
        "symbols": binance_live.live_symbols(),
        "clock": clock,
    }
    if live_link:
        return BinanceSpotAdapter.live_link(client, **kw)
    return BinanceSpotAdapter(client, **kw)


def _live_account(adapter: str = binance_live.ADAPTER) -> Account:
    return Account(
        id=uuid.UUID(int=7),
        alias="real-100",
        kind="LIVE",
        venue_key="BINANCE",
        broker_adapter=adapter,
        base_currency="USDT",
        status="ACTIVE",
        reconciliation_status="OK",
        last_reconciled_at=NOW,
        created_at=NOW,
    )


def test_the_real_venue_is_reached_only_through_the_live_link() -> None:
    emu, pem = _emulator()
    client = httpx.AsyncClient(transport=emu.transport())
    args: dict[str, Any] = {
        "api_key": "k",
        "signer": Ed25519Signer(pem.encode()),
        "symbols": {},
        "clock": FrozenClock(NOW),
    }
    with pytest.raises(ValueError, match="only talks to the Binance Spot Test Network"):
        BinanceSpotAdapter(client, base_url=LIVE_BASES[0], **args)
    with pytest.raises(ValueError, match="only talks to the real Binance Spot venue"):
        BinanceSpotAdapter(client, base_url=TESTNET_BASES[0], _live=True, **args)
    real = BinanceSpotAdapter.live_link(client, **args)
    caps = real.capabilities()
    assert (real.base, real.root, real.live) == (LIVE_BASES[0], "https://api.binance.com", True)
    assert (caps.name, caps.is_live, caps.is_simulated) == (binance_live.ADAPTER, True, False)
    test = BinanceSpotAdapter(client, **args).capabilities()
    assert (test.name, test.is_live, test.is_simulated) == ("binance_spot_testnet", False, True)


def test_the_contract_counts_only_for_the_code_that_ran_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every file that shapes what is sent is hashed: a change to any of them is new code."""
    names = {Path(f).name for f in binance_check.CODE_FILES}
    assert names == {"binance_spot.py", "binance_check.py", "base.py", "money.py"}
    assert binance_check.CONTRACT_VERSION.endswith(f"+{binance_check.adapter_code_hash()}")
    copies = []
    for i, name in enumerate(binance_check.CODE_FILES):
        copy = tmp_path / f"{i}.py"
        copy.write_bytes(Path(name).read_bytes())
        copies.append(str(copy))
    monkeypatch.setattr(binance_check, "CODE_FILES", tuple(copies))
    same = binance_check.adapter_code_hash()
    assert same == binance_check.CONTRACT_VERSION.rsplit("+", 1)[1]
    for copy in copies:  # one changed byte in any file
        original = Path(copy).read_bytes()
        Path(copy).write_bytes(original + b"\n")
        assert binance_check.adapter_code_hash() != same, copy
        Path(copy).write_bytes(original)


def test_the_live_link_is_built_only_while_the_server_locks_are_open(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem, _pub = keypair()
    clock = FrozenClock(NOW)
    keyed = settings.model_copy(
        update={
            "live_venues": ["BINANCE"],
            "binance_live_api_key": SecretStr("live-api-key"),
            "binance_live_private_key": SecretStr(pem),
        }
    )
    assert binance_live.build(keyed, clock) is None  # lock 1: no live adapter in this build
    monkeypatch.setattr(authority, "LIVE_ADAPTERS", frozenset({binance_live.ADAPTER}))
    assert binance_live.build(keyed.model_copy(update={"live_venues": []}), clock) is None
    unkeyed = keyed.model_copy(update={"binance_live_private_key": None})
    assert binance_live.build(unkeyed, clock) is None
    assert (
        binance_live.build(
            keyed.model_copy(update={"binance_live_private_key": SecretStr("x")}), clock
        )
        is None
    )
    built = binance_live.build(keyed, clock)
    assert built is not None and built.adapter.live
    assert built.key_fingerprint == live.key_fingerprint("live-api-key")
    assert set(built.adapter.symbols) >= {PAXG}


async def test_the_keys_own_permissions_are_read_from_the_real_host_only() -> None:
    emu, pem = _emulator()
    with pytest.raises(Exception, match="the Spot Test Network has no /sapi"):
        await _adapter(emu, pem, live_link=False).key_restrictions()
    real = _adapter(emu, pem)
    steps, restrictions = await binance_check.run_live_check(real)
    assert all(s.passed for s in steps), steps
    assert [s.name for s in steps][-1] == "the book is readable"
    assert restrictions == emu.key_permissions
    assert len(live.judge_key(restrictions)) == 3  # the documented example is refused
    assert not any("POST" in r or "DELETE" in r for r in emu.requests)  # no order, ever
    emu.key_permissions = dict(GOOD_KEY)
    _steps, good = await binance_check.run_live_check(real)
    assert good is not None and live.judge_key(good) == []
    with pytest.raises(ValueError, match="needs the live link"):
        await binance_check.run_live_check(_adapter(emu, pem, live_link=False))


async def test_the_venues_book_is_read_with_its_round_trip() -> None:
    emu, pem = _emulator()
    quote = await _adapter(emu, pem).book_ticker(PAXG)
    assert (quote.bid, quote.ask) == (D("4100.00"), D("4100.50"))
    assert timedelta(0) <= quote.round_trip < timedelta(seconds=1)  # timed by a monotonic clock
    with pytest.raises(Exception, match="not traded on Binance"):
        await _adapter(emu, pem).book_ticker("SPOT:ETH/USDT@BINANCE")
    for bid, ask in (("NaN", "4100.5"), ("4100", "Infinity"), ("4101", "4100"), ("0", "1")):
        emu.book["PAXGUSDT"] = (D(bid), D(ask))
        with pytest.raises(BrokerUnavailableError, match="not usable"):
            await _adapter(emu, pem).book_ticker(PAXG)


def test_a_real_money_check_is_priced_only_by_the_book_just_read() -> None:
    real = account(kind="LIVE")
    stored = market()  # a fresh stored quote: DELAYED, and never enough for real money
    assert r09_price_validity(context(account=real, market=stored), POLICY).status is Status.FAIL
    nothing = venue_market(stored, None)
    assert (nothing.quote_label, nothing.bid, nothing.ask) == (DataLabel.UNAVAILABLE, None, None)
    quick = VenueQuote(PAXG, D("4100"), D("4100.5"), NOW, timedelta(milliseconds=300))
    fresh = venue_market(stored, quick)
    assert (fresh.quote_label, fresh.bid, fresh.quote_ts) == (DataLabel.LIVE, D("4100"), NOW)

    def r09(m: Any, *, seconds: int = 0, kind: str = "LIVE") -> Status:
        ctx = context(account=account(kind=kind), market=m, now=NOW + timedelta(seconds=seconds))
        return r09_price_validity(ctx, POLICY).status

    assert r09(fresh) is Status.PASS
    assert r09(fresh, seconds=5) is Status.PASS
    assert r09(fresh, seconds=6) is Status.FAIL  # 5 s for real money, whatever the policy says
    slow = venue_market(stored, replace(quick, round_trip=timedelta(seconds=3)))
    assert slow.quote_label is DataLabel.STALE and r09(slow) is Status.FAIL
    backwards = venue_market(stored, replace(quick, round_trip=timedelta(seconds=-1)))
    assert backwards.quote_label is DataLabel.STALE
    assert r09(fresh, seconds=-1) is Status.FAIL  # stamped in the future: the clock moved
    assert r09(nothing) is Status.FAIL
    # paper keeps the policy's limit and its stored quotes
    assert r09(replace(stored, quote_ts=NOW), seconds=60, kind="PAPER") is Status.PASS


def test_a_real_money_entry_is_not_sent_on_a_price_older_than_5_seconds() -> None:
    assert live_price_too_old(NOW, NOW) is None
    assert live_price_too_old(NOW, NOW + timedelta(seconds=5)) is None
    late = live_price_too_old(NOW, NOW + timedelta(seconds=31))
    assert late is not None and "31.0s old when the order was ready to send" in late
    assert live_price_too_old(NOW, NOW - timedelta(seconds=1)) is not None


async def test_the_registry_serves_a_live_account_only_through_the_live_link(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    emu, pem = _emulator()
    registry = env.svc.brokers
    real = _live_account()
    with pytest.raises(ConflictError, match="live trading is not available in this build"):
        registry.for_account(real)
    registry.live = LiveBroker(_adapter(emu, pem), live.key_fingerprint(emu.api_key))
    with pytest.raises(ConflictError):  # built, but lock 1 is shut at the moment of lookup
        registry.for_account(real)
    assert await registry.venue_quote(real, PAXG) is None
    monkeypatch.setattr(authority, "LIVE_ADAPTERS", frozenset({binance_live.ADAPTER}))
    assert registry.for_account(real) is registry.live.adapter
    quote = await registry.venue_quote(real, PAXG)
    assert quote is not None and quote.ask == D("4100.50")
    emu.faults.append("connect")
    assert await registry.venue_quote(real, PAXG) is None  # unreadable: R09 then refuses
    emu.book["PAXGUSDT"] = (D("NaN"), D("4100.5"))
    assert await registry.venue_quote(real, PAXG) is None  # garbled: no price, no exception
    async with env.db.connect() as conn:
        paper_account = await get_account(conn, account_id=env.account_id)
    assert await registry.venue_quote(paper_account, PAXG) is None  # paper: stored quotes


async def test_the_worker_checks_the_live_key_and_records_what_it_read(env: Env) -> None:
    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="test")
    ctx.services["trading"] = env.svc
    assert await check_live_key(ctx) is None  # this build: no live link, nothing happens

    async def checks() -> int:
        async with env.db.connect() as conn:
            return int(await conn.scalar(select(func.count()).select_from(live_key_checks)) or 0)

    assert await checks() == 0
    emu, pem = _emulator()
    env.svc.brokers.live = LiveBroker(_adapter(emu, pem), live.key_fingerprint(emu.api_key))
    refused = await check_live_key(ctx)
    assert refused is not None and not refused.passed and len(refused.failures) == 3
    emu.key_permissions = dict(GOOD_KEY)
    passed = await check_live_key(ctx)
    assert passed is not None and passed.passed
    assert passed.key_fingerprint == live.key_fingerprint(emu.api_key)
    emu.faults.append("connect")
    assert await check_live_key(ctx) is None  # unreadable: a warning, nothing recorded
    assert await checks() == 2
    async with env.db.connect() as conn:
        warning = (
            await conn.execute(
                select(incidents).where(incidents.c.dedup_key == "live.key_unreadable")
            )
        ).one()
    assert warning.severity == "WARNING"
    emu.faults.clear()
    assert await check_live_key(ctx) is not None  # read again: the warning is resolved
    async with env.db.connect() as conn:
        status = await conn.scalar(
            select(incidents.c.status).where(incidents.c.dedup_key == "live.key_unreadable")
        )
    assert status == "RESOLVED"
