"""Telegram handlers with simulated updates (live bot BLOCKED on token, B-05)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.core.redaction import contains_secret
from cosmic.db.engine import Database
from cosmic.db.schema import users
from cosmic.db.schema_execution import approvals, paper_orders, trade_intents
from cosmic.identity import mfa
from cosmic.notify.telegram.handlers import Reply, TelegramHandlers, Update
from cosmic.notify.telegram.linking import create_link_code
from cosmic.trading import authority
from cosmic.trading.modes import Mode
from cosmic.trading.proposals import get_intent
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
    assert "Decision guide (not advice):" in msgs[0].text  # ADR-033: six signs with the card
    assert "Cost of this trade:" in msgs[0].text and "Size:" in msgs[0].text
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


async def test_malformed_callbacks_are_refused_quietly(env: Env) -> None:
    """Buttons carry our own references; anything else (a forged or truncated callback) is an
    unknown action, never an error, and changes nothing."""
    h, _ = await linked(env)
    p = await env.propose()
    for data in ("a:garbage", "r:", "rc:12", "v:not-a-uuid:details", "zz:1", ":"):
        reply = await h.handle(Update(TG_ID, TG_ID, "private", callback_data=data))
        assert reply[0].text == "Unknown action.", data
    async with env.db.connect() as conn:
        assert (await get_intent(conn, p.intent_id)).status == "PROPOSED"


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


async def test_a_decided_trade_can_never_be_approved_afterwards(env: Env) -> None:
    """REQUEST CHANGES and REJECT are final for that proposal: an approval begun before the
    decision dies with it, and a new one cannot start (directive Phase 11, scenario 2)."""
    h, secret = await linked(env)
    p = await env.propose()
    confirm = (await h.handle(Update(TG_ID, TG_ID, "private", callback_data=f"a:{p.intent_id}")))[0]
    rc = await h.handle(Update(TG_ID, TG_ID, "private", callback_data=f"rc:{p.intent_id}"))
    assert "CHANGES REQUESTED" in rc[0].text
    stale = await h.handle(
        Update(TG_ID, TG_ID, "private", callback_data=buttons(confirm)["CONFIRM APPROVAL"])
    )
    assert stale[0].text == "This trade is CHANGES_REQUESTED; nothing to approve."
    late = await h.handle(
        Update(TG_ID, TG_ID, "private", text=mfa.code_at(secret, env.clock.now()))
    )
    assert "Free text is never an approval" in late[0].text  # no approval is waiting any more
    for kind in ("a", "r", "rc"):
        again = await h.handle(
            Update(TG_ID, TG_ID, "private", callback_data=f"{kind}:{p.intent_id}")
        )
        assert again[0].text.startswith("Not done") and "nothing to approve" in again[0].text
    q = await env.propose()
    await h.handle(Update(TG_ID, TG_ID, "private", callback_data=f"r:{q.intent_id}"))
    with pytest.raises(ConflictError, match="REJECTED"):
        await env.approve(q.intent_id)  # the portal refuses it too
    await env.cycle(2)  # the execution authority comes round: nothing may be sent
    async with env.db.connect() as conn:
        statuses = dict(
            (await conn.execute(select(trade_intents.c.id, trade_intents.c.status))).all()
        )
    assert statuses == {p.intent_id: "CHANGES_REQUESTED", q.intent_id: "REJECTED"}
    assert await env.count(approvals, approvals.c.decision == "APPROVED") == 0
    assert await env.count(paper_orders) == 0


async def test_a_code_sent_after_the_trade_was_decided_is_not_used_up(env: Env) -> None:
    """The bot asked for the code, then the trade was decided (here: REQUEST CHANGES). The code
    that arrives next approves nothing and stays unused, so it still works for another trade."""
    h, secret = await linked(env)
    p = await env.propose()
    confirm = (await h.handle(Update(TG_ID, TG_ID, "private", callback_data=f"a:{p.intent_id}")))[0]
    asked = await h.handle(
        Update(TG_ID, TG_ID, "private", callback_data=buttons(confirm)["CONFIRM APPROVAL"])
    )
    assert "6-digit" in asked[0].text
    await h.handle(Update(TG_ID, TG_ID, "private", callback_data=f"rc:{p.intent_id}"))
    code = mfa.code_at(secret, env.clock.now())
    late = await h.handle(Update(TG_ID, TG_ID, "private", text=code))
    assert late[0].text == (
        "This trade is CHANGES_REQUESTED; nothing to approve. Your code was not used."
    )
    q = await env.propose()
    confirm = (await h.handle(Update(TG_ID, TG_ID, "private", callback_data=f"a:{q.intent_id}")))[0]
    await h.handle(
        Update(TG_ID, TG_ID, "private", callback_data=buttons(confirm)["CONFIRM APPROVAL"])
    )
    done = await h.handle(Update(TG_ID, TG_ID, "private", text=code))  # the same code
    # names the trade, the account and its environment (DIR-02)
    assert done[0].text.startswith(f"{q.trade_code} on paper-main (SIMULATED · PAPER — ")
    assert ": APPROVED (single use" in done[0].text
    assert await intent_status_of(env, p.intent_id) == "CHANGES_REQUESTED"
    assert await intent_status_of(env, q.intent_id) == "APPROVED"


async def intent_status_of(env: Env, intent_id: uuid.UUID) -> str:
    async with env.db.connect() as conn:
        return (await get_intent(conn, intent_id)).status


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


async def test_a_linked_user_without_the_capability_is_refused(env: Env) -> None:
    """Directive Phase 11: a linked viewer can read but not act."""
    from cosmic.identity.capabilities import Role
    from tests.helpers import make_user, principal_for

    viewer = await make_user(env.db, env.clock, username="viewer", roles=[Role.VIEWER])
    async with env.db.transaction() as conn:
        code = await create_link_code(
            conn, principal=principal_for(viewer, env.clock), now=env.clock.now()
        )
    h = TelegramHandlers(env.db, env.clock, env.settings, env.svc)
    assert (
        "Linked"
        in (await h.handle(Update(OTHER_ID, OTHER_ID, "private", text=f"/link {code}")))[0].text
    )
    stop = await h.handle(Update(OTHER_ID, OTHER_ID, "private", text="/stop someone is trading"))
    assert "not allowed" in stop[0].text
    p = await env.propose()
    approve = await h.handle(
        Update(OTHER_ID, OTHER_ID, "private", callback_data=f"a:{p.intent_id}")
    )
    assert approve[0].text.startswith("Not done")
    async with env.db.connect() as conn:
        assert (await get_intent(conn, p.intent_id)).status == "PROPOSED"
        assert (await authority.get_state(conn)).mode is Mode.PAPER_TRADING
