"""Telegram handlers with simulated updates (live bot BLOCKED on token, B-05)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.core.redaction import contains_secret
from cosmic.db.engine import Database
from cosmic.db.schema import users
from cosmic.db.schema_execution import paper_orders, trade_intents
from cosmic.identity import mfa
from cosmic.notify.telegram.handlers import Reply, TelegramHandlers, Update
from cosmic.notify.telegram.linking import create_link_code
from cosmic.trading import authority
from cosmic.trading.modes import Mode
from tests.trading_env import Env, build_env

TG_ID = 555_000_111
OTHER_ID = 999_000_222


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def linked(env: Env) -> tuple[TelegramHandlers, str]:
    secret = mfa.generate_secret()
    async with env.db.transaction() as conn:
        await conn.execute(
            users.update()
            .where(users.c.id == env.owner.user_id)
            .values(
                mfa_enabled=True,
                mfa_secret_enc=mfa.encrypt_secret(env.settings.secret_key_bytes, secret),
            )
        )
        code = await create_link_code(conn, principal=env.principal(), now=env.clock.now())
    h = TelegramHandlers(env.db, env.clock, env.settings, env.svc)
    r = await h.handle(Update(TG_ID, TG_ID, "private", text=f"/link {code}"))
    assert "Linked" in r[0].text
    again = await h.handle(Update(OTHER_ID, OTHER_ID, "private", text=f"/link {code}"))
    assert "Not linked" in again[0].text  # single use
    return h, secret


def buttons(r: Reply) -> dict[str, str]:
    return {label: data for row in r.buttons for label, data in row}


async def test_private_only_and_unknown_users(env: Env) -> None:
    h, _ = await linked(env)
    assert await h.handle(Update(TG_ID, -100, "group", text="/status")) == []
    r = await h.handle(Update(OTHER_ID, OTHER_ID, "private", text="/status"))
    assert "only serves its linked operator" in r[0].text
    status = await h.handle(Update(TG_ID, TG_ID, "private", text="/status"))
    assert "PAPER_TRADING" in status[0].text and "DISABLED" in status[0].text


async def test_full_telegram_approval_with_totp(env: Env) -> None:
    h, secret = await linked(env)
    p = await env.propose()
    msgs = await h.handle(Update(TG_ID, TG_ID, "private", text="/approvals"))
    b = buttons(msgs[0])
    assert {
        "APPROVE EXACT TRADE",
        "REJECT",
        "REQUEST CHANGES",
        "EVIDENCE",
        "COUNTERARGUMENTS",
        "RISK",
        "PORTFOLIO IMPACT",
        "REVIEW DETAILS",
    } <= set(b)
    assert "SIMULATED" in msgs[0].text
    for label in ("REVIEW DETAILS", "EVIDENCE", "COUNTERARGUMENTS", "RISK", "PORTFOLIO IMPACT"):
        assert (await h.handle(Update(TG_ID, TG_ID, "private", callback_data=b[label])))[0].text
    # Free text is never approval.
    for txt in ("ok", "looks good", "approve", "yes"):
        r = await h.handle(Update(TG_ID, TG_ID, "private", text=txt))
        assert "never an approval" in r[0].text
    confirm = (
        await h.handle(Update(TG_ID, TG_ID, "private", callback_data=b["APPROVE EXACT TRADE"]))
    )[0]
    assert (
        "CONFIRM EXACT TRADE" in confirm.text
        and "BUY 0.004" in confirm.text
        and "83000" in confirm.text
    )
    ask = await h.handle(
        Update(TG_ID, TG_ID, "private", callback_data=buttons(confirm)["CONFIRM APPROVAL"])
    )
    assert "authenticator code" in ask[0].text
    assert (await env.count(trade_intents, trade_intents.c.status == "APPROVED")) == 0
    wrong = await h.handle(Update(TG_ID, TG_ID, "private", text="000000"))
    assert "Nothing was approved" in wrong[0].text
    env.clock.advance(seconds=30)
    ok = await h.handle(Update(TG_ID, TG_ID, "private", text=mfa.code_at(secret, env.clock.now())))
    assert "APPROVED" in ok[0].text
    async with env.db.connect() as conn:
        assert (
            await conn.execute(
                select(trade_intents.c.status).where(trade_intents.c.id == p.intent_id)
            )
        ).scalar_one() == "APPROVED"
    # Replays: the same confirm button and the approve button no longer work.
    replay = await h.handle(
        Update(TG_ID, TG_ID, "private", callback_data=buttons(confirm)["CONFIRM APPROVAL"])
    )
    assert "no longer valid" in replay[0].text
    again = await h.handle(Update(TG_ID, TG_ID, "private", callback_data=b["APPROVE EXACT TRADE"]))
    assert "Not done" in again[0].text
    await env.cycle()
    assert await env.count(paper_orders) == 1


async def test_other_user_cannot_use_callbacks(env: Env) -> None:
    h, _ = await linked(env)
    p = await env.propose()
    r = await h.handle(Update(OTHER_ID, OTHER_ID, "private", callback_data=f"a:{p.intent_id}"))
    assert "only serves its linked operator" in r[0].text
    forged = await h.handle(Update(TG_ID, TG_ID, "private", callback_data=f"c:{uuid.uuid4()}"))
    assert "no longer valid" in forged[0].text


async def test_confirmation_expires(env: Env) -> None:
    h, secret = await linked(env)
    p = await env.propose()
    confirm = (await h.handle(Update(TG_ID, TG_ID, "private", callback_data=f"a:{p.intent_id}")))[0]
    await h.handle(
        Update(TG_ID, TG_ID, "private", callback_data=buttons(confirm)["CONFIRM APPROVAL"])
    )
    env.clock.advance(minutes=4)
    late = await h.handle(
        Update(TG_ID, TG_ID, "private", text=mfa.code_at(secret, env.clock.now()))
    )
    assert "expired" in late[0].text or "never an approval" in late[0].text
    assert await env.count(trade_intents, trade_intents.c.status == "APPROVED") == 0


async def test_reject_and_request_changes(env: Env) -> None:
    h, _ = await linked(env)
    p = await env.propose()
    r = await h.handle(Update(TG_ID, TG_ID, "private", callback_data=f"r:{p.intent_id}"))
    assert "REJECTED" in r[0].text
    async with env.db.connect() as conn:
        assert (await conn.execute(select(trade_intents.c.status))).scalar_one() == "REJECTED"


async def test_emergency_stop_from_telegram(env: Env) -> None:
    h, _ = await linked(env)
    r = await h.handle(Update(TG_ID, TG_ID, "private", text="/stop market looks broken"))
    assert "NOT closed" in r[0].text
    done = await h.handle(Update(TG_ID, TG_ID, "private", callback_data="k"))
    assert "EMERGENCY STOP active" in done[0].text
    async with env.db.connect() as conn:
        assert (await authority.get_state(conn)).mode is Mode.EMERGENCY_STOP


async def test_replies_never_contain_secrets(env: Env) -> None:
    h, _ = await linked(env)
    await env.propose()
    replies: list[Reply] = []
    for text in ("/help", "/status", "/approvals"):
        replies += await h.handle(Update(TG_ID, TG_ID, "private", text=text))
    blob = "\n".join(r.text for r in replies)
    assert not contains_secret(blob)
    assert str(env.account_id) not in blob  # account identifiers stay internal; aliases only


async def test_admin_link_code_links_and_is_audited_as_cli(env: Env) -> None:
    from datetime import timedelta

    from cosmic.audit.service import Actor, ActorType
    from cosmic.core.errors import ConflictError
    from cosmic.db.schema import audit_events
    from cosmic.notify.telegram.linking import create_link_code_for

    cli = Actor(ActorType.CLI, "telegram-link-code")
    async with env.db.transaction() as conn:
        for bad in (timedelta(seconds=30), timedelta(minutes=61)):
            with pytest.raises(ConflictError, match="between 1 and 60"):
                await create_link_code_for(
                    conn, user_id=env.owner.user_id, actor=cli, now=env.clock.now(), ttl=bad
                )
        code = await create_link_code_for(
            conn,
            user_id=env.owner.user_id,
            actor=cli,
            now=env.clock.now(),
            ttl=timedelta(minutes=30),
        )
    h = TelegramHandlers(env.db, env.clock, env.settings, env.svc)
    env.clock.advance(minutes=25)  # still valid inside its 30-minute lifetime
    r = await h.handle(Update(TG_ID, TG_ID, "private", text=f"/link {code}"))
    assert "Linked" in r[0].text, r[0].text
    async with env.db.connect() as conn:
        tg = (await conn.execute(select(users.c.telegram_user_id))).scalar_one()
        row = (
            await conn.execute(
                select(audit_events.c.actor_type, audit_events.c.payload).where(
                    audit_events.c.action == "telegram.link_code_created"
                )
            )
        ).one()
    assert tg == TG_ID and row.actor_type == "CLI" and row.payload["ttl_minutes"] == 30
