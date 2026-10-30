"""The real-money locks (ADR-034, S40a): each one refuses on its own, the database refuses a
real-money buy without the owner's activation, and every way an activation ends takes a live
mode with it. No test opens lock 1 for real: ``LIVE_ADAPTERS`` is patched, never changed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.audit.service import Actor
from cosmic.core.config import Settings, load_settings
from cosmic.core.errors import ConfigError, ConflictError, PermissionDeniedError
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.db.schema_execution import trading_mandates
from cosmic.db.schema_trading import authority_state, live_activations
from cosmic.identity.capabilities import Role
from cosmic.trading import authority, golive, killswitch, live, queries
from cosmic.trading.authority import LIVE_ADAPTERS
from cosmic.trading.brokers.binance_check import CONTRACT_VERSION, Step
from cosmic.trading.modes import Mode
from cosmic.trading.risk.checks import Status, r20_live_activation
from cosmic.trading.risk.context import LiveActivationState
from cosmic.trading.sandbox import record_contract_run
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login, make_user, principal_for
from tests.risk_factory import NOW, account, context, held, open_order, request
from tests.trading_env import Env, build_env

D = Decimal
ADAPTER = "binance_spot_live"
GOOD_KEY = {
    "ipRestrict": True,
    "createTime": 1698645219000,
    "enableReading": True,
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
    "enableMargin": False,
    "enableFutures": False,
    "permitsUniversalTransfer": False,
    "enableVanillaOptions": False,
    "enableFixApiTrade": False,
    "enableFixReadOnly": True,
    "enableSpotAndMarginTrading": True,
    "enablePortfolioMarginTrading": False,
}


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


def test_this_build_has_no_live_adapter() -> None:
    """Lock 1 is a reviewed code change, never made by S40a."""
    assert frozenset() == LIVE_ADAPTERS


def test_the_key_judge_passes_only_a_key_that_trades_spot_from_one_address() -> None:
    assert live.judge_key(GOOD_KEY) == []
    for flag in live.KEY_MUST_ALLOW:
        assert live.judge_key({**GOOD_KEY, flag: False}) == [
            f"the key must have {live.KEY_WORDS[flag]} on"
        ]
    for flag in live.KEY_MUST_REFUSE:
        assert live.judge_key({**GOOD_KEY, flag: True}) == [
            f"the key must have {live.KEY_WORDS[flag]} off"
        ]
    missing = {k: v for k, v in GOOD_KEY.items() if k != "enableWithdrawals"}
    assert live.judge_key(missing) == [
        "the key must have withdrawals off (enableWithdrawals not reported)"
    ]
    assert live.judge_key({**GOOD_KEY, "ipRestrict": "true"}) == [  # a string is not a yes
        "the key must have an IP restriction on (ipRestrict not reported)"
    ]
    # Binance's documented example key: no IP restriction, no spot trading, portfolio margin
    example = {**GOOD_KEY, "ipRestrict": False, "enableSpotAndMarginTrading": False}
    example["enablePortfolioMarginTrading"] = True
    assert len(live.judge_key(example)) == 3
    assert live.key_fingerprint("abc") == "ba7816bf8f01cfea"
    assert len(live.judge_key({})) == len(live.KEY_MUST_ALLOW) + len(live.KEY_MUST_REFUSE)


def test_only_binance_can_be_named_a_live_venue() -> None:
    def load(**extra: str) -> Settings:
        return load_settings(
            {
                "COSMIC_ENV": "LOCAL_DEVELOPMENT",
                "COSMIC_SECRET_KEY": "insecure-dev-live-locks",
                **extra,
            }
        )

    assert load().live_venues == []
    assert load(COSMIC_LIVE_VENUES='["BINANCE"]').live_venues == ["BINANCE"]
    with pytest.raises(ConfigError, match="COSMIC_LIVE_VENUES may only name"):
        load(COSMIC_LIVE_VENUES='["KRAKEN"]')


def test_r20_needs_an_activation_and_keeps_real_money_within_its_cap() -> None:
    def r20(**kw: Any) -> Any:
        return r20_live_activation(context(**kw), None)  # type: ignore[arg-type]

    assert r20().status is Status.PASS  # paper: not real money
    real = account(kind="LIVE")
    assert r20(account=real).status is Status.FAIL
    cap = LiveActivationState(D(100), NOW + timedelta(days=1), contract_current=True)
    small = request(quantity=D("0.0005"), limit_price=D("80000"))  # 40.00
    # holdings 0.00025 × 83000 = 20.75, a resting entry 0.0002 × 80000 = 16.00
    busy = account(
        kind="LIVE",
        positions=held("0.00025"),
        open_orders=(open_order(qty="0.0002", price="80000"),),
    )
    ok = r20(account=busy, request=small, live_activation=cap)
    assert ok.status is Status.PASS and "76.75 committed" in ok.message
    big = r20(
        account=busy,
        request=request(quantity=D("0.001"), limit_price=D("80000")),
        live_activation=cap,
    )
    assert big.status is Status.FAIL and "above the activation's cap 100.00" in big.message
    # an exit resting at the venue is not new exposure
    exiting = account(
        kind="LIVE",
        open_orders=(open_order(side="SELL", qty="1", price="80000", reduce_only=True),),
    )
    assert r20(account=exiting, request=small, live_activation=cap).status is Status.PASS
    unmarked = account(kind="LIVE", positions=held("0.00025", mark=None))
    assert r20(account=unmarked, request=small, live_activation=cap).status is Status.UNKNOWN
    expired = LiveActivationState(D(100), NOW, contract_current=True)
    assert r20(account=real, request=small, live_activation=expired).status is Status.FAIL
    changed = LiveActivationState(D(100), NOW + timedelta(days=1))  # the code has no run
    stale = r20(account=busy, request=small, live_activation=changed)
    assert stale.status is Status.FAIL and "no PASSED contract run" in stale.message


async def _live_account(env: Env, *, reconciled: str = "OK") -> uuid.UUID:
    account_id = uuid7()
    async with env.db.transaction() as conn:
        venue = await conn.scalar(text("SELECT id FROM venues WHERE key = 'BINANCE'"))
        await conn.execute(
            text(
                "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, "
                "status, reconciliation_status, created_at, updated_at, key_fingerprint) VALUES "
                "(:id, 'real-100', 'LIVE', :v, :a, 'USDT', 'ACTIVE', :r, now(), now(), :k)"
            ),
            {
                "id": account_id,
                "v": venue,
                "a": ADAPTER,
                "r": reconciled,
                "k": live.key_fingerprint("public-key"),  # the key _key_check reads
            },
        )
    return account_id


def _real_now() -> datetime:
    """The database bounds the application's clock by its own (migration 0016), so its guards
    are tested with activations in real time, not the test clock's past."""
    return datetime.now(UTC)


async def _activation(
    env: Env,
    account_id: uuid.UUID,
    *,
    cap: str = "100",
    days: int = 30,
    at: datetime | None = None,
) -> uuid.UUID:
    """An activation row as the database sees it (live.activate is tested on its own)."""
    activation_id = uuid7()
    now = at or env.clock.now()
    async with env.db.transaction() as conn:
        await conn.execute(
            live_activations.insert().values(
                id=activation_id,
                activated_by=env.owner.user_id,
                readiness_snapshot={},
                activated_at=now,
                account_id=account_id,
                cap=D(cap),
                expires_at=now + timedelta(days=days),
                reason="rehearsal passed; start with $100",
            )
        )
    return activation_id


async def _key_check(env: Env, restrictions: dict[str, Any] | None = None) -> live.KeyCheck:
    async with env.db.transaction() as conn:
        return await live.record_key_check(
            conn,
            adapter=ADAPTER,
            fingerprint=live.key_fingerprint("public-key"),
            restrictions=restrictions or GOOD_KEY,
            actor=Actor.system("test"),
            now=env.clock.now(),
        )


def _open_settings(env: Env) -> Settings:
    return env.settings.model_copy(
        update={"max_authority_mode": "APPROVAL_REQUIRED", "live_venues": ["BINANCE"]}
    )


def _open_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locks 1 and 4 opened for the test only: a patched adapter set, and every gate A–H
    reported PASSED."""
    monkeypatch.setattr(live, "LIVE_ADAPTERS", frozenset({ADAPTER}))
    monkeypatch.setattr(authority, "LIVE_ADAPTERS", frozenset({ADAPTER}))
    real = golive.readiness

    async def passed(conn: Any, now: datetime) -> list[golive.GateStatus]:
        out = []
        for g in await real(conn, now):
            if g.key != golive.OWNER_GATE or await live.active_activations(conn, now):
                g = golive.GateStatus(g.key, g.title, "PASSED", [], None, g.how)
            out.append(g)
        return out

    monkeypatch.setattr(golive, "readiness", passed)


async def _activate(env: Env, account_id: uuid.UUID, settings: Settings, **kw: Any) -> Any:
    async with env.db.transaction() as conn:
        return await live.activate(
            conn,
            principal=env.principal(),
            settings=settings,
            account_id=account_id,
            cap=kw.get("cap", D(100)),
            days=kw.get("days", 30),
            reason=kw.get("reason", "rehearsal passed; start with $100"),
            now=env.clock.now(),
        )


async def _contract_run(env: Env, version: str = CONTRACT_VERSION) -> None:
    """A PASSED contract run on the Binance test network, for this code unless told otherwise."""
    async with env.db.transaction() as conn:
        await record_contract_run(
            conn,
            steps=[Step("connect", True)],
            key_fingerprint="40ad1a36e4c400a2",
            code_version=version,
            started_at=env.clock.now(),
            finished_at=env.clock.now(),
            actor=Actor.system("test"),
        )


async def test_an_activation_is_refused_by_each_lock_on_its_own(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = await _live_account(env)
    await _key_check(env)
    shut = env.settings.model_copy(
        update={"max_authority_mode": "PAPER_TRADING", "live_venues": []}
    )
    with pytest.raises(ConflictError) as closed:  # this build, as deployed: locks 1–4 shut
        await _activate(env, account_id, shut)
    for lock in ("(lock 1)", "(lock 2)", "(lock 3)", "(lock 4)"):
        assert lock in str(closed.value)
    assert "(lock 5)" not in str(closed.value)  # the key check passed
    _open_locks(monkeypatch)
    opened = _open_settings(env)
    await _contract_run(env, "binance-spot-contract-1")  # a run of other code does not count
    with pytest.raises(ConflictError, match=r"has no PASSED contract run .*\(lock 1\)"):
        await _activate(env, account_id, opened)
    await _contract_run(env)
    refusals = {
        "lock 2": (opened.model_copy(update={"max_authority_mode": "PAPER_TRADING"}), {}),
        "lock 3": (opened.model_copy(update={"live_venues": []}), {}),
        "the cap must be": (opened, {"cap": D("1000.01")}),
        "an activation lasts": (opened, {"days": 91}),
        "a written reason": (opened, {"reason": "go"}),
    }
    for expected, (settings, kw) in refusals.items():
        with pytest.raises(ConflictError, match=expected):
            await _activate(env, account_id, settings, **kw)
    env.clock.advance(hours=25)  # lock 5: a passing check older than a day
    with pytest.raises(ConflictError, match="more than 24 hours old"):
        await _activate(env, account_id, opened)
    env.clock.advance(seconds=1)
    await _key_check(env, {**GOOD_KEY, "enableWithdrawals": True})
    with pytest.raises(ConflictError, match="withdrawals off"):
        await _activate(env, account_id, opened)
    async with env.db.connect() as conn:  # while setting up: a warning, the rehearsals go on
        severity = await conn.scalar(select(incidents.c.severity))
    assert severity == "WARNING"
    env.clock.advance(seconds=1)
    await _key_check(env)
    with pytest.raises(PermissionDeniedError):
        async with env.db.transaction() as conn:  # an analyst cannot
            await live.activate(
                conn,
                principal=principal_for(env.owner, env.clock, roles=("ANALYST",)),
                settings=opened,
                account_id=account_id,
                cap=D(100),
                days=30,
                reason="not mine to give",
                now=env.clock.now(),
            )
    made = await _activate(env, account_id, opened)  # every lock open: it is allowed
    assert (made.cap, made.alias, made.active(env.clock.now())) == (D(100), "real-100", True)
    with pytest.raises(ConflictError, match="already has an active activation"):
        await _activate(env, account_id, opened)
    async with env.db.connect() as conn:
        actions = [
            r.action
            for r in await conn.execute(
                text("SELECT action FROM audit_events WHERE action LIKE 'live.%' ORDER BY seq")
            )
        ]
    assert actions[-1] == "live.activated" and actions.count("live.key_checked") == 3


async def test_an_unreconciled_or_paper_account_cannot_be_activated(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_locks(monkeypatch)
    await _key_check(env)
    account_id = await _live_account(env, reconciled="REQUIRED")
    with pytest.raises(ConflictError, match="reconciliation is REQUIRED"):
        await _activate(env, account_id, _open_settings(env))
    with pytest.raises(ConflictError, match="only a LIVE account"):
        await _activate(env, env.account_id, _open_settings(env))
    with pytest.raises(Exception, match="only for a LIVE account"):
        await _activation(env, env.account_id)  # the database refuses it too


async def test_the_database_refuses_a_real_money_buy_without_an_activation(env: Env) -> None:
    """Whatever the application does: a paper order is cloned onto a LIVE account."""
    env.clock.advance(seconds=1)
    await env.set_market("82990.00", "83000.00")
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(1)
    account_id = await _live_account(env)
    async with env.db.connect() as conn:
        source = await conn.scalar(text("SELECT id FROM orders LIMIT 1"))
    assert source is not None

    async def clone(
        qty: str,
        price: str,
        *,
        reduce_only: bool = False,
        side: str = "BUY",
        purpose: str | None = None,
    ) -> uuid.UUID:
        """The paper entry (a BUY) copied onto the LIVE account, each under a copy of its intent
        (an intent has one ENTRY); with ``reduce_only`` it is an EXIT unless told otherwise."""
        order_id, intent_id = uuid7(), uuid7()
        async with env.db.transaction() as conn:
            await conn.execute(
                text(
                    "INSERT INTO trade_intents SELECT * FROM jsonb_populate_record("
                    "NULL::trade_intents, (SELECT to_jsonb(t) || jsonb_build_object("
                    "'id', CAST(:i AS uuid), 'account_id', CAST(:a AS uuid), "
                    "'trade_code', CAST(:code AS text)) FROM trade_intents t "
                    "JOIN orders o ON o.intent_id = t.id WHERE o.id = :src))"
                ),
                {"i": str(intent_id), "a": str(account_id), "code": str(intent_id), "src": source},
            )
            await conn.execute(
                text(
                    "INSERT INTO orders SELECT * FROM jsonb_populate_record(NULL::orders, "
                    "(SELECT to_jsonb(o) || jsonb_build_object('id', CAST(:id AS uuid), "
                    "'intent_id', CAST(:i AS uuid), "
                    "'account_id', CAST(:a AS uuid), 'client_order_id', CAST(:id AS text), "
                    "'quantity', CAST(:q AS numeric), 'limit_price', CAST(:p AS numeric), "
                    "'reduce_only', CAST(:r AS boolean), 'created_at', CAST(:at AS timestamptz), "
                    "'purpose', CAST(:purpose AS text), 'side', CAST(:side AS text)) "
                    "FROM orders o WHERE o.id = :src))"
                ),
                {
                    "id": str(order_id),
                    "i": str(intent_id),
                    "a": str(account_id),
                    "q": qty,
                    "p": price,
                    "r": reduce_only,
                    "at": env.clock.now(),
                    "purpose": purpose or ("EXIT" if reduce_only else "ENTRY"),
                    "side": side,
                    "src": source,
                },
            )
        return order_id

    refused = "needs the owner's live activation"
    with pytest.raises(Exception, match=refused):
        await clone("0.001", "83000")
    await clone("0.001", "83000", reduce_only=True, side="SELL")  # an exit is never blocked
    await clone("0.001", "80000", reduce_only=True, side="SELL", purpose="STOP_LOSS")
    with pytest.raises(Exception, match=refused):  # a buy adds exposure, whatever its flags
        await clone("0.001", "83000", reduce_only=True)
    with pytest.raises(Exception, match=refused):
        await clone("0.001", "83000", reduce_only=True, purpose="ENTRY")
    activation = await _activation(env, account_id, cap="100", at=_real_now())
    sent = await clone("0.001", "83000")  # 83.00, within 100
    with pytest.raises(Exception, match="above the live activation's cap"):
        await clone("0.002", "83000")  # 166.00
    async with env.db.connect() as conn:
        paper = await conn.scalar(text("SELECT id FROM accounts WHERE alias = 'paper-main'"))
    for statement in (
        "UPDATE orders SET quantity = 1 WHERE id = :id",
        "UPDATE orders SET side = 'SELL' WHERE id = :id",
        "UPDATE orders SET purpose = 'EXIT', reduce_only = true WHERE id = :id",
    ):
        with pytest.raises(Exception, match="never change"):
            async with env.db.transaction() as conn:
                await conn.execute(text(statement), {"id": sent})
    with pytest.raises(Exception, match="never change"):  # nor leave the LIVE account
        async with env.db.transaction() as conn:
            await conn.execute(
                text("UPDATE orders SET account_id = :p WHERE id = :id"), {"p": paper, "id": sent}
            )
    async with env.db.transaction() as conn:  # status changes go on as before
        await conn.execute(
            text("UPDATE orders SET status_version = status_version + 1 WHERE id = :id"),
            {"id": sent},
        )
    async with env.db.transaction() as conn:
        assert (
            await live.revoke(
                conn,
                activation_id=activation,
                actor=Actor.system("test"),
                reason="test",
                now=env.clock.now(),
            )
            == 1
        )
    with pytest.raises(Exception, match=refused):
        await clone("0.001", "83000")
    # Expired by the database's clock: an order stamped in the past cannot reach back into it.
    expired = await _activation(env, account_id, days=1, at=_real_now() - timedelta(days=2))
    async with env.db.connect() as conn:
        window = await conn.scalar(
            text("SELECT activated_at FROM live_activations WHERE id = :id"), {"id": expired}
        )
    env.clock.set(window + timedelta(hours=1))  # inside its window by the application's clock
    with pytest.raises(Exception, match=refused):
        await clone("0.001", "83000")


async def test_a_real_money_mandate_must_ask_within_the_activation(env: Env) -> None:
    account_id = await _live_account(env)
    now = _real_now()

    async def mandate(*, mode: str = "ASK", capital: str = "100", days: int = 30) -> None:
        async with env.db.transaction() as conn:
            await conn.execute(
                trading_mandates.insert().values(
                    id=uuid7(),
                    account_id=account_id,
                    strategy_key="ema_200_trend",
                    timeframe="1d",
                    instruments=["SPOT:PAXG/USDT@BINANCE"],
                    max_loss_per_trade=D(2),
                    max_trades_per_day=1,
                    max_open_positions=1,
                    daily_loss_limit=D(6),
                    status="ACTIVE",
                    granted_by=env.owner.user_id,
                    granted_at=now,
                    expires_at=now + timedelta(days=days),
                    capital=D(capital),
                    risk_pct=D(2),
                    mode=mode,
                )
            )

    refused = "must ask before each trade"
    with pytest.raises(Exception, match=refused):
        await mandate()  # no activation
    await _activation(env, account_id, cap="100", days=31, at=now - timedelta(minutes=1))
    with pytest.raises(Exception, match=refused):
        await mandate(mode="AUTO")  # never a standing approval for real money
    with pytest.raises(Exception, match=refused):
        await mandate(capital="100.01")
    with pytest.raises(Exception, match=refused):
        await mandate(days=31)  # outlives the activation
    await mandate()


async def test_activations_revocations_and_key_checks_are_append_only(env: Env) -> None:
    account_id = await _live_account(env)
    activation = await _activation(env, account_id)
    await _key_check(env)
    for statement in (
        "UPDATE live_activations SET cap = 1000",
        "DELETE FROM live_activations",
        "UPDATE live_key_checks SET passed = true",
        "DELETE FROM live_key_checks",
    ):
        with pytest.raises(Exception, match=r"append-only|permission denied"):
            async with env.db.transaction() as conn:
                await conn.execute(text(statement))
    async with env.db.transaction() as conn:
        actor = Actor.system("test")
        now = env.clock.now()
        assert (
            await live.revoke(conn, activation_id=activation, actor=actor, reason="first", now=now)
            == 1
        )
        assert (
            await live.revoke(conn, activation_id=activation, actor=actor, reason="second", now=now)
            == 0
        )
    with pytest.raises(Exception, match=r"append-only|permission denied"):
        async with env.db.transaction() as conn:
            await conn.execute(text("DELETE FROM live_activation_revocations"))
    analyst = await make_user(env.db, env.clock, "analyst", roles=[Role.ANALYST])
    with pytest.raises(Exception, match="only an active owner"):
        async with env.db.transaction() as conn:
            await conn.execute(
                live_activations.insert().values(
                    id=uuid7(),
                    activated_by=analyst.user_id,
                    readiness_snapshot={},
                    activated_at=env.clock.now(),
                    account_id=account_id,
                    cap=D(100),
                    expires_at=env.clock.now() + timedelta(days=1),
                    reason="not an owner",
                )
            )
    with pytest.raises(Exception, match="live_activations_expiry_check"):
        await _activation(env, account_id, days=91)


async def _go_live(env: Env, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """A LIVE account activated and the mode at APPROVAL_REQUIRED, every lock opened for the
    test."""
    _open_locks(monkeypatch)
    await _contract_run(env)
    account_id = await _live_account(env)
    await _key_check(env)
    settings = _open_settings(env)
    made = await _activate(env, account_id, settings)
    async with env.db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=env.principal(),
            target=Mode.APPROVAL_REQUIRED,
            reason="every lock open; one trade at a time",
            now=env.clock.now(),
            settings=settings,
        )
    async with env.db.connect() as conn:
        row = (await conn.execute(select(authority_state))).one()
    assert (row.mode, row.live_activation_id) == ("APPROVAL_REQUIRED", made.id)
    return made.id


async def test_the_banner_names_the_mode_when_every_lock_is_open_but_it_is_not_live(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_locks(monkeypatch)
    await _contract_run(env)
    account_id = await _live_account(env)
    await _key_check(env)
    await _activate(env, account_id, _open_settings(env))
    async with env.db.connect() as conn:
        banner = await queries.authority_banner(conn)
    assert banner["live_enabled"] is False
    assert banner["live_reason"] == "the mode is PAPER_TRADING, not a live mode"


async def test_revoking_the_last_activation_ends_the_live_mode(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    activation = await _go_live(env, monkeypatch)
    async with env.db.transaction() as conn:
        await live.revoke_by_owner(
            conn,
            principal=env.principal(mfa_fresh=False),  # restricting needs no fresh code
            activation_id=activation,
            reason="pause real money",
            now=env.clock.now(),
        )
    async with env.db.connect() as conn:
        row = (await conn.execute(select(authority_state))).one()
    assert (row.mode, row.live_activation_id) == ("PAPER_TRADING", None)


async def test_an_expired_activation_ends_the_live_mode_at_the_next_sweep(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _go_live(env, monkeypatch)
    async with env.db.transaction() as conn:
        assert not await live.enforce(conn, env.clock.now())
    env.clock.advance(days=30)
    await _key_check(env)  # the daily check kept up: it is the expiry that ends it
    async with env.db.transaction() as conn:
        assert await live.enforce(conn, env.clock.now())
        assert (await authority.get_state(conn)).mode is Mode.PAPER_TRADING
        (ended,) = await live.activations(conn)
    assert ended.revoked_at is None and not ended.active(env.clock.now())


async def test_an_overdue_key_check_ends_real_money(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock 5 holds while money is live, not only at the start: the daily check must keep
    passing."""
    await _go_live(env, monkeypatch)
    env.clock.advance(hours=25)
    async with env.db.transaction() as conn:
        assert not await live.enforce(conn, env.clock.now())  # within the 26 hours
    env.clock.advance(hours=2)
    async with env.db.transaction() as conn:
        assert await live.enforce(conn, env.clock.now())
        assert (await authority.get_state(conn)).mode is Mode.PAPER_TRADING
        (ended,) = await live.activations(conn)
    assert ended.revoke_reason == "no passing key check in the last 26 hours"


async def test_a_release_that_changes_the_adapter_ends_real_money(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contract run counts only for the code that ran it: new code stops real money until the
    contract is run again, even with an activation in force."""
    await _go_live(env, monkeypatch)
    monkeypatch.setattr(live, "CONTRACT_VERSION", "binance-spot-contract-1+changedcode0")
    async with env.db.transaction() as conn:
        assert await live.enforce(conn, env.clock.now())
        assert (await authority.get_state(conn)).mode is Mode.PAPER_TRADING
        (ended,) = await live.activations(conn)
    assert ended.revoke_reason == "the adapter's code changed: it has no PASSED contract run"


async def test_a_failed_step_of_the_live_check_fails_the_record(env: Env) -> None:
    async with env.db.transaction() as conn:
        check = await live.record_key_check(
            conn,
            adapter=ADAPTER,
            fingerprint=live.key_fingerprint("public-key"),
            restrictions=GOOD_KEY,  # the key itself is fine
            actor=Actor.system("test"),
            now=env.clock.now(),
            also_failed=["the account can trade spot"],
        )
    assert not check.passed
    assert check.failures == ["the live check failed: the account can trade spot"]


async def test_a_failed_key_check_ends_real_money_and_raises_an_incident(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _go_live(env, monkeypatch)
    check = await _key_check(env, {**GOOD_KEY, "ipRestrict": False})
    assert not check.passed and check.failures == ["the key must have an IP restriction on"]
    async with env.db.connect() as conn:
        assert await live.active_activations(conn, env.clock.now()) == []
        assert (await authority.get_state(conn)).mode is Mode.PAPER_TRADING
        incident = (
            await conn.execute(select(incidents).where(incidents.c.category == "SECURITY"))
        ).one()
    assert incident.severity == "CRITICAL"
    stored = check.restrictions
    assert stored["ipRestrict"] is False and "createTime" not in stored  # known flags only


async def test_the_emergency_stop_ends_every_activation(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _go_live(env, monkeypatch)
    async with env.db.connect() as conn:
        account_id = await conn.scalar(text("SELECT id FROM accounts WHERE kind = 'LIVE'"))
    # another process's clock a minute ahead stamped this one
    ahead = await _activation(env, account_id, at=env.clock.now() + timedelta(minutes=1))
    async with env.db.transaction() as conn:
        await killswitch.activate(
            conn, actor=Actor.system("test"), reason="drill", now=env.clock.now()
        )
    async with env.db.connect() as conn:
        ended = await live.activations(conn)
        assert await live.unrevoked_activations(conn, env.clock.now()) == []
    assert {a.revoke_reason for a in ended} == {"emergency stop: drill"} and len(ended) == 2
    assert ahead in {a.id for a in ended}
    assert Mode.APPROVAL_REQUIRED not in killswitch.RESUMABLE
    with pytest.raises(ConflictError, match="the system is in EMERGENCY_STOP"):
        await _activate(env, account_id, _open_settings(env))


async def test_without_the_live_venue_no_live_mode_is_possible(env: Env) -> None:
    async with env.db.connect() as conn:
        unmet = await authority.live_preconditions(conn, env.clock.now(), live_venues=[])
        named = await authority.live_preconditions(conn, env.clock.now(), live_venues=["BINANCE"])
        gates = {g.key: g for g in await golive.readiness(conn, env.clock.now())}
    assert "COSMIC_LIVE_VENUES names no venue for real money" in unmet
    assert "COSMIC_LIVE_VENUES names no venue for real money" not in named
    assert gates["I"].status == "BLOCKED"
    assert "no live adapter exists in this build (ADR-034 lock 1)" in gates["I"].reasons


async def test_a_live_mode_shows_as_live_and_needs_its_venue_at_startup(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The banner reads the stored mode; a restart without COSMIC_LIVE_VENUES leaves it."""
    await _go_live(env, monkeypatch)
    async with env.db.connect() as conn:
        assert (await queries.authority_banner(conn))["live_enabled"] is True
    async with env.db.transaction() as conn:  # same env file: nothing to do
        kept = await authority.enforce_cap(conn, settings=_open_settings(env), now=env.clock.now())
    assert kept.mode is Mode.APPROVAL_REQUIRED
    no_venue = _open_settings(env).model_copy(update={"live_venues": []})
    async with env.db.transaction() as conn:
        state = await authority.enforce_cap(conn, settings=no_venue, now=env.clock.now())
    assert state.mode is Mode.PROTECTION_ONLY
    async with env.db.connect() as conn:
        title = await conn.scalar(select(incidents.c.title))
    assert title == "Stored mode APPROVAL_REQUIRED without a live venue"


async def test_the_golive_page_shows_each_lock(env: Env) -> None:
    account_id = await _live_account(env)
    await _activation(env, account_id)
    check = await _key_check(env)  # shown even though no live adapter is registered
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/golive")).text
    assert "The locks between this system and real money" in page
    assert "none: this build has no live adapter" in page
    assert "COSMIC_LIVE_VENUES: none" in page
    assert "COSMIC_MAX_AUTHORITY_MODE is PAPER_TRADING" in page
    assert "EACH TRADE" in page and "LOCKED" in page
    assert "real-100 [LIVE · real money] up to 100.00 USDT" in page  # gate I and the history
    assert "Live activations" in page and ">active<" in page
    assert f"passed {check.key_fingerprint} at 2026-10-10 21:06 UTC" in page
