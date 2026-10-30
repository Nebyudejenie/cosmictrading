"""The real-money account and the owner's controls on the go-live page (ADR-034, S40c-1): the
account opens only from the live link with a passing check of its own key, names that key for
good, and is activated and ended only by the owner. Every venue call goes to the emulator."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.db.engine import Database
from cosmic.db.schema import incidents, jobs
from cosmic.db.schema_trading import accounts
from cosmic.identity.capabilities import Role
from cosmic.trading import authority, ledger, live
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers import binance_live
from cosmic.trading.brokers.base import VenueBalance
from cosmic.trading.brokers.binance_live import LiveBroker
from cosmic.trading.jobs import check_live_key, open_live
from cosmic.web.app import create_app
from cosmic.worker.context import WorkerContext
from tests.helpers import client_for, csrf_from, full_login, make_user
from tests.trading.test_live_link import _adapter, _emulator
from tests.trading.test_live_locks import (
    _activate,
    _contract_run,
    _key_check,
    _open_locks,
    _open_settings,
)
from tests.trading_env import Env, build_env

D = Decimal
KEY = "public-key"  # the key _key_check reads
BALANCES = [VenueBalance("USDT", D("250"), D("240")), VenueBalance("PAXG", D("0.01"), D("0.01"))]
GOOD = dict.fromkeys(live.KEY_MUST_ALLOW, True) | dict.fromkeys(live.KEY_MUST_REFUSE, False)


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def _open(env: Env, **kw: Any) -> uuid.UUID:
    args: dict[str, Any] = {
        "alias": "real-binance",
        "managed_capital": D(100),
        "balances": BALANCES,
        "key_fingerprint": live.key_fingerprint(KEY),
        "now": env.clock.now(),
        "actor": Actor.user(env.owner.user_id),
    }
    args.update(kw)
    async with env.db.transaction() as conn:
        account_id = await live.open_live_account(conn, **args)
        # As the worker's reconciliation would leave it once Binance agrees.
        await conn.execute(
            text(
                "UPDATE accounts SET reconciliation_status = 'OK', last_reconciled_at = now() "
                "WHERE id = :id"
            ),
            {"id": account_id},
        )
    return account_id


async def test_a_real_money_account_opens_only_after_every_check_before_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConflictError, match=r"not registered in this build \(lock 1\)"):
        await _open(env)
    _open_locks(monkeypatch)
    with pytest.raises(ConflictError, match="no PASSED contract run"):
        await _open(env)
    await _contract_run(env)
    with pytest.raises(ConflictError, match=r"no passing check in the last 24 hours: .*\(lock 5\)"):
        await _open(env)
    await _key_check(env)
    other = live.key_fingerprint("another-key")
    with pytest.raises(ConflictError, match="this key has no passing check"):
        await _open(env, key_fingerprint=other)  # a check of another key does not count
    for kw, problem in (
        ({"alias": "binance"}, "alias: real-"),
        ({"managed_capital": D("1000.01")}, "at most 1000"),
        ({"managed_capital": D(0)}, "above 0"),
        ({"managed_capital": D("240.01")}, "has 240 USDT free"),
    ):
        with pytest.raises(ValidationFailedError, match=problem):
            await _open(env, **kw)
    account_id = await _open(env)
    async with env.db.connect() as conn:
        account = await get_account(conn, account_id=account_id)
        cash = await ledger.book_balance(conn, account_id, "CASH", "USDT")
        usdt = await ledger.book_balance(conn, account_id, "UNMANAGED", "USDT")
        paxg = await ledger.book_balance(conn, account_id, "UNMANAGED", "PAXG")
    assert (account.kind, account.broker_adapter, account.key_fingerprint) == (
        "LIVE",
        binance_live.ADAPTER,
        live.key_fingerprint(KEY),
    )
    assert (cash, usdt, paxg) == (D(100), D(150), D("0.01"))  # all of it accounted for
    with pytest.raises(ConflictError, match="already open"):
        await _open(env, alias="real-second")
    for statement in (
        "UPDATE accounts SET key_fingerprint = 'ffffffffffffffff' WHERE kind = 'LIVE'",
        "UPDATE accounts SET kind = 'PAPER', broker_adapter = 'paper' WHERE kind = 'LIVE'",
    ):
        with pytest.raises(Exception, match=r"never changes|stays a real-money account"):
            async with env.db.transaction() as conn:
                await conn.execute(text(statement))


async def test_lock_5_counts_only_a_check_of_the_accounts_own_key(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_locks(monkeypatch)
    await _contract_run(env)
    await _key_check(env)
    account_id = await _open(env)
    env.clock.advance(hours=25)  # the account's own check is now too old
    async with env.db.transaction() as conn:  # a fresh, passing check of another key
        await live.record_key_check(
            conn,
            adapter=binance_live.ADAPTER,
            fingerprint=live.key_fingerprint("another-key"),
            restrictions=GOOD,
            actor=Actor.system("test"),
            now=env.clock.now(),
        )
    with pytest.raises(ConflictError, match="more than 24 hours old"):
        await _activate(env, account_id, _open_settings(env))
    await _key_check(env)
    made = await _activate(env, account_id, _open_settings(env))
    assert made.key_fingerprint == live.key_fingerprint(KEY)
    async with env.db.transaction() as conn:  # a failed check of another key ends nothing
        bad = await live.record_key_check(
            conn,
            adapter=binance_live.ADAPTER,
            fingerprint=live.key_fingerprint("another-key"),
            restrictions={**GOOD, "enableWithdrawals": True},
            actor=Actor.system("test"),
            now=env.clock.now(),
        )
        assert not bad.passed
        assert [a.id for a in await live.active_activations(conn, env.clock.now())] == [made.id]


async def test_only_the_accounts_own_key_may_trade_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key rotated on the worker never trades an account opened with another key, and its
    activation ends at the worker's next check."""
    _open_locks(monkeypatch)
    await _contract_run(env)
    await _key_check(env)
    account_id = await _open(env)
    made = await _activate(env, account_id, _open_settings(env))
    emu, pem = _emulator()
    env.svc.brokers.live = LiveBroker(_adapter(emu, pem), live.key_fingerprint(emu.api_key))
    async with env.db.connect() as conn:
        account = await get_account(conn, account_id=account_id)
    with pytest.raises(ConflictError, match="is not the key real-binance was opened with"):
        env.svc.brokers.for_account(account)
    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="test")
    ctx.services["trading"] = env.svc
    emu.key_permissions = dict(GOOD)
    assert await check_live_key(ctx) is not None
    async with env.db.connect() as conn:
        assert await live.active_activations(conn, env.clock.now()) == []
        ended = await live.get_activation(conn, made.id)
    assert ended is not None
    assert (
        ended.revoke_reason == "the worker's live key is not the key this account was opened with"
    )


async def test_the_worker_opens_it_only_through_the_live_link(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="test")
    ctx.services["trading"] = env.svc
    payload = {"alias": "real-binance", "managed_capital": "100", "user_id": str(env.owner.user_id)}
    assert await open_live(ctx, payload) is None  # this build: no live link
    async with env.db.connect() as conn:
        why = await conn.scalar(
            select(incidents.c.title).where(
                incidents.c.dedup_key == "trading.live_not_opened:real-binance"
            )
        )
    assert why is not None and why.endswith("real-money account was not opened")
    _open_locks(monkeypatch)
    await _contract_run(env)
    emu, pem = _emulator()
    emu.balances["BNB"] = [D("0.5"), D(0)]  # the owner's own coin: not this system's to count
    broker = LiveBroker(_adapter(emu, pem), live.key_fingerprint(emu.api_key))
    env.svc.brokers.live = broker
    async with env.db.transaction() as conn:
        await live.record_key_check(
            conn,
            adapter=binance_live.ADAPTER,
            fingerprint=broker.key_fingerprint,
            restrictions=GOOD,
            actor=Actor.system("test"),
            now=env.clock.now(),
        )
    account_id = await open_live(ctx, payload)
    assert account_id is not None
    async with env.db.connect() as conn:
        opened = await get_account(conn, account_id=account_id)
        managed = await ledger.book_balance(conn, account_id, "CASH", "USDT")
        paxg = await ledger.book_balance(conn, account_id, "UNMANAGED", "PAXG")
    assert opened.key_fingerprint == broker.key_fingerprint and managed == D(100)
    assert paxg == D(2)  # the emulator's PAXG, recorded as not managed
    assert opened.reconciliation_status == "OK"  # Binance agrees, BNB and all


async def test_the_owner_activates_and_ends_real_money_on_the_go_live_page(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/golive")).text
        assert "Opening one becomes possible once locks 1 and 3 are open" in page
        assert 'action="/golive/live-account"' not in page
        token = csrf_from(page)
        shut = await c.post(
            "/golive/live-account",
            data={"csrf_token": token, "alias": "real-binance", "capital": "100"},
        )
        assert shut.status_code == 409 and "locks 1 and 3" in shut.text
        _open_locks(monkeypatch)
        await _contract_run(env)
        await _key_check(env)
        open_app = create_app(_open_settings(env), db=env.db, clock=env.clock)
        async with client_for(open_app) as o:
            await full_login(o, env.owner, env.clock)
            page = (await o.get("/golive")).text
            assert 'action="/golive/live-account"' in page
            token = csrf_from(page)
            queued = await o.post(
                "/golive/live-account",
                data={"csrf_token": token, "alias": "real-binance", "capital": "100"},
            )
            assert queued.status_code == 303
            await _open(env)
            page = (await o.get("/golive")).text
            assert 'action="/golive/activate"' in page
            refused = await o.post(
                "/golive/activate",
                data={
                    "csrf_token": token,
                    "account": "real-binance",
                    "cap": "100",
                    "days": "91",
                    "reason": "start with $100",
                },
            )
            assert refused.status_code == 409 and "an activation lasts 1 to 90 days" in refused.text
            nan = await o.post(
                "/golive/activate",
                data={
                    "csrf_token": token,
                    "account": "real-binance",
                    "cap": "NaN",
                    "days": "30",
                    "reason": "start with $100",
                },
            )
            assert nan.status_code == 422
            done = await o.post(
                "/golive/activate",
                data={
                    "csrf_token": token,
                    "account": "real-binance",
                    "cap": "100",
                    "days": "30",
                    "reason": "start with $100",
                },
            )
            assert done.status_code == 303, done.text
            page = (await o.get("/golive")).text
            assert 'action="/golive/activate"' not in page  # one activation at a time
            assert "End real money on real-binance now" in page
            async with env.db.connect() as conn:
                (active,) = await live.active_activations(conn, env.clock.now())
            unknown = await o.post(
                "/golive/revoke",
                data={"csrf_token": token, "activation_id": str(uuid.uuid4()), "reason": "x" * 5},
            )
            assert unknown.status_code == 404
            ended = await o.post(
                "/golive/revoke",
                data={"csrf_token": token, "activation_id": str(active.id), "reason": "pause"},
            )
            assert ended.status_code == 303
    async with env.db.connect() as conn:
        assert await live.active_activations(conn, env.clock.now()) == []
        queued_jobs = [
            r.payload["alias"]
            for r in await conn.execute(
                select(jobs.c.payload).where(jobs.c.kind == "trading.open_live_account")
            )
        ]
        refusals = await conn.scalar(
            text("SELECT count(*) FROM audit_events WHERE action = 'live.activation_refused'")
        )
    assert queued_jobs == ["real-binance"]
    assert refusals == 1  # the refused attempt stays on record


async def test_only_the_owner_may_open_or_activate_and_anyone_signed_in_cannot_end_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_locks(monkeypatch)
    await _contract_run(env)
    await _key_check(env)
    account_id = await _open(env)
    made = await _activate(env, account_id, _open_settings(env))
    viewer = await make_user(env.db, env.clock, username="viewer", roles=[Role.VIEWER])
    app = create_app(_open_settings(env), db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, viewer, env.clock)
        token = csrf_from((await c.get("/golive")).text)
        for path, data in (
            ("/golive/live-account", {"alias": "real-x", "capital": "10"}),
            ("/golive/activate", {"account": "real-binance", "cap": "1", "days": "1"}),
            ("/golive/revoke", {"activation_id": str(made.id), "reason": "not mine"}),
        ):
            r = await c.post(path, data={"csrf_token": token, **data})
            assert r.status_code == 403, (path, r.status_code)
    async with env.db.connect() as conn:
        assert [a.id for a in await live.active_activations(conn, env.clock.now())] == [made.id]
        assert await conn.scalar(select(accounts.c.alias).where(accounts.c.kind == "LIVE"))
    assert authority.LIVE_ADAPTERS  # patched for this test only
