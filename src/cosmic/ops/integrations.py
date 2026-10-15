"""Which integrations are configured, as reported by the processes that hold their secrets.

The web process holds no AI keys, no Telegram token and no test-network keys (least
privilege): the worker (AI and Binance test-network keys) and the bot (Telegram token) report
"CONFIGURED"/"NOT_CONFIGURED" — never a value — in their heartbeats. A report older than two
minutes is not trusted: the status becomes UNKNOWN rather than a guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.config import Settings
from cosmic.db.schema import worker_heartbeats

REPORT_MAX_AGE = timedelta(minutes=2)
ALLOWED_VALUES = frozenset({"CONFIGURED", "NOT_CONFIGURED"})


async def reported(conn: AsyncConnection, now: datetime) -> dict[str, str]:
    """Freshest report per integration from recent heartbeats (any process)."""
    rows = await conn.execute(
        select(worker_heartbeats.c.info)
        .where(worker_heartbeats.c.last_beat_at >= now - REPORT_MAX_AGE)
        .order_by(desc(worker_heartbeats.c.last_beat_at))
    )
    out: dict[str, str] = {}
    for r in rows:
        for key, value in ((r.info or {}).get("integrations") or {}).items():
            if key not in out and value in ALLOWED_VALUES:
                out[str(key)] = str(value)
    return out


async def integration_view(
    conn: AsyncConnection, settings: Settings, now: datetime
) -> dict[str, str]:
    """What the portal shows: CONFIGURED if this process or a fresh report says so."""
    own = settings.integration_status()
    rep = await reported(conn, now)
    view: dict[str, str] = {}
    for key, value in own.items():
        if value == "CONFIGURED" or rep.get(key) == "CONFIGURED":
            view[key] = "CONFIGURED"
        elif key in rep:
            view[key] = rep[key]
        elif key in ("live_broker", "cloudflare_tunnel"):
            view[key] = value  # not reported by any process; static in this build
        elif key == "telegram":
            view[key] = "NOT_CONFIGURED"  # the bot runs only with a token; no report = not running
        else:
            view[key] = "UNKNOWN"  # nobody holding the secret has reported recently
    return view


AI_KEYS = ("groq", "gemini", "openrouter")
WORKER_KEYS = (*AI_KEYS, "binance_testnet")


def worker_report(settings: Settings) -> dict[str, str]:
    """What the worker reports: the AI and test-network keys it holds (presence only)."""
    status = settings.integration_status()
    return {k: status[k] for k in WORKER_KEYS}
