"""AT-16: credentials never appear in frontend assets, rendered pages or ordinary logs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.logs import configure_logging
from cosmic.core.redaction import SECRETS, contains_secret
from cosmic.db.engine import Database
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login, make_user

FAKE = {
    "groq_api_key": "gsk_" + "G" * 40,
    "openrouter_api_key": "sk-or-v1-" + "O" * 40,
    "telegram_bot_token": "123456789:" + "T" * 35,
    "metrics_token": "metrics-token-" + "M" * 20,
    "binance_testnet_api_key": "B" * 64,
}
PAGES = [
    "/",
    "/sources",
    "/market",
    "/approvals",
    "/portfolio",
    "/orders",
    "/ops",
    "/audit",
    "/ai",
    "/settings/telegram",
    "/proposals/new",
    "/api/v1/status",
    "/api/v1/sources",
]


@pytest.fixture
def secret_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={k: SecretStr(v) for k, v in FAKE.items()})


async def test_pages_assets_and_logs_never_contain_credentials(
    secret_settings: Settings, db: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    SECRETS.register(secret_settings.secret_values())
    configure_logging("DEBUG", json_output=True, process="test")
    clock = FrozenClock(datetime(2026, 10, 10, 21, 6, tzinfo=UTC))
    app = create_app(secret_settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    bodies: list[str] = []
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        for path in PAGES:
            r = await c.get(path)
            assert r.status_code == 200, (path, r.status_code)
            bodies.append(r.text)
            bodies.append(json.dumps(dict(r.headers)))
        bodies.append((await c.get("/static/style.css")).text)
    blob = "\n".join(bodies)
    for name, value in FAKE.items():
        assert value not in blob, name
    db_password = (
        secret_settings.db_app_password.get_secret_value()
        if secret_settings.db_app_password
        else ""
    )
    assert db_password and db_password not in blob
    assert not contains_secret(blob)
    assert (
        "configured (value never shown)" in blob
    )  # the AI page acknowledges keys without revealing them
    logs = capsys.readouterr().out
    for value in FAKE.values():
        assert value not in logs


def test_static_and_template_sources_contain_no_credentials() -> None:
    web = Path(__file__).resolve().parents[2] / "src" / "cosmic" / "web"
    for path in list((web / "templates").glob("*.html")) + list((web / "static").glob("*")):
        assert not contains_secret(path.read_text()), path
