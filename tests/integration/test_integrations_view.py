"""Integration status as the web process sees it without holding any AI key or bot token."""

from __future__ import annotations

from pydantic import SecretStr

from cosmic.ai.admin import provider_overview
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.ops.heartbeat import beat
from cosmic.ops.integrations import integration_view, worker_report


async def test_view_follows_fresh_reports_only(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    web = settings.model_copy(update={"groq_api_key": None, "telegram_bot_token": None})
    async with db.connect() as conn:
        v = await integration_view(conn, web, clock.now())
    assert v["groq"] == "UNKNOWN"  # nobody holding AI keys has reported: do not guess
    assert v["telegram"] == "NOT_CONFIGURED" and v["live_broker"] == "NOT_CONFIGURED"

    worker = settings.model_copy(update={"groq_api_key": SecretStr("gsk_" + "w" * 40)})
    async with db.transaction() as conn:
        await beat(
            conn,
            worker_id="w-1",
            process="worker",
            started_at=clock.now(),
            now=clock.now(),
            info={"integrations": worker_report(worker)},
        )
        await beat(
            conn,
            worker_id="b-1",
            process="bot",
            started_at=clock.now(),
            now=clock.now(),
            info={"integrations": {"telegram": "CONFIGURED", "gemini": "sk-not-a-status"}},
        )
    async with db.connect() as conn:
        v = await integration_view(conn, web, clock.now())
        overview = {
            p["provider"].key: p for p in await provider_overview(conn, web, clock.now(), v)
        }
    assert v["groq"] == "CONFIGURED" and v["openrouter"] == "NOT_CONFIGURED"
    assert v["telegram"] == "CONFIGURED"
    assert v["gemini"] == "NOT_CONFIGURED"  # an invalid reported value is ignored
    assert overview["groq"]["key_configured"] is True
    assert overview["groq"]["status"] == "UNAVAILABLE"  # key present, but no attestation yet
    assert overview["openrouter"]["status"] == "NOT_CONFIGURED"

    clock.advance(minutes=3)  # reports go stale
    async with db.connect() as conn:
        v = await integration_view(conn, web, clock.now())
    assert v["groq"] == "UNKNOWN" and v["telegram"] == "NOT_CONFIGURED"


async def test_a_process_holding_the_key_always_knows(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    dev = settings.model_copy(update={"groq_api_key": SecretStr("gsk_" + "d" * 40)})
    async with db.connect() as conn:
        assert (await integration_view(conn, dev, clock.now()))["groq"] == "CONFIGURED"
