from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.errors import ValidationFailedError
from cosmic.db.engine import Database
from cosmic.db.schema_trading import risk_policies
from cosmic.trading.authority import live_preconditions
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.brokers.binance_check import Step
from cosmic.trading.golive import readiness, record_gate
from cosmic.trading.sandbox import record_contract_run
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user
from tests.trading_env import CAPTURE, build_env

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "cosmic" / "web" / "templates"


async def test_command_center_answers_the_questions(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db)
    await env.propose()
    app = create_app(settings, db=db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/")).text
    for heading in (
        "Needs your attention",
        "Accounts",
        "Prices",
        "Connections",
        "Trades awaiting approval",
        "What changed",
        "Verified vs inferred",
        "Work",
        "System health",
        "Account health",
        "Risks and incidents",
    ):
        assert heading in page, heading
    assert "1 proposal(s) await your decision" in page
    assert "no recent worker heartbeat" in page  # honest: no worker runs in this test
    assert "A healthy application does not mean a healthy trading account" in page
    assert "SIMULATED" in page
    # The directive's Overview (Phase 2): each account apart, prices with their age, links.
    assert re.search(r'<a href="/portfolio">paper-main</a></th><td>.*SIMULATED.*PAPER</td>', page)
    assert "Totals are never added across accounts" in page
    assert re.search(r"SPOT:BTC/USD@COINBASE</a></th>.*DELAYED.*old</td>", page)
    assert "has never reported" in page  # no HFM bridge in this test
    assert "not reported by the worker" in page and "NO QUALIFIED STRATEGY" in page


async def test_golive_readiness_never_passes_without_broker(
    settings: Settings, db: Database
) -> None:
    clock = FrozenClock(CAPTURE)
    async with db.transaction() as conn:
        for gate in "ABCDEFGH":
            await record_gate(
                conn,
                gate=gate,
                status="PASSED",
                evidence={"note": "pretend everything passed"},
                actor=Actor.system("test"),
                now=clock.now(),
            )
        with pytest.raises(ValidationFailedError, match="owner's own activation"):
            await record_gate(
                conn,
                gate="I",
                status="PASSED",
                evidence={"note": "a command can never activate live trading"},
                actor=Actor.system("test"),
                now=clock.now(),
            )
        gates = {g.key: g for g in await readiness(conn, clock.now())}
        unmet = await live_preconditions(conn, clock.now(), live_venues=[])
    for key in "ABDEH":
        assert gates[key].status == "PASSED", key  # recorded evidence passes these
    for key in "CFGI":
        assert gates[key].status == "BLOCKED", key  # evidence alone cannot unlock live trading
        assert f"go-live gate {key} has not passed" in unmet
    assert not [u for u in unmet for key in "ABDEH" if f"gate {key} has" in u]
    assert "no contract run on the Binance test network yet" in gates["C"].reasons
    assert gates["I"].how.startswith("Your activation with a fresh code")
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        page = (await c.get("/golive")).text
    assert "Live trading is <strong>DISABLED</strong>" in page and "B-07" in page
    assert "What passes it" in page and "tests/integration/test_database_outage.py" in page


async def test_gate_c_shows_the_sandbox_contract_runs(db: Database) -> None:
    clock = FrozenClock(CAPTURE)
    async with db.transaction() as conn:
        for passed, at in ((True, clock.now()), (False, clock.now() + timedelta(days=1))):
            await record_contract_run(
                conn,
                steps=[Step("connect", passed)],
                key_fingerprint="0123456789abcdef",
                code_version="test",
                started_at=at,
                finished_at=at,
                actor=Actor.system("test"),
                adapter=mt5_bridge.ADAPTER,
            )
        gates = {g.key: g for g in await readiness(conn, clock.now())}
    reasons = " | ".join(gates["C"].reasons)
    assert "contract run FAILED on the HFM demo" in reasons  # the latest run, as it is
    assert f"last PASSED on the HFM demo: {CAPTURE:%Y-%m-%d}" in reasons
    assert "no live adapter exists yet" in reasons and gates["C"].status == "BLOCKED"


async def test_metrics_require_token(settings: Settings, db: Database, clock: FrozenClock) -> None:
    app = create_app(settings, db=db, clock=clock)
    token = settings.metrics_token.get_secret_value() if settings.metrics_token else ""
    assert token, "dev .env provides a metrics token"
    async with client_for(app) as c:
        assert (await c.get("/internal/metrics")).status_code == 404
        assert (
            await c.get("/internal/metrics", headers={"authorization": "Bearer wrong"})
        ).status_code == 404
        r = await c.get("/internal/metrics", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200 and "cosmic_database_size_bytes" in r.text


async def test_the_reconciliation_metric_names_the_environment(
    settings: Settings, db: Database
) -> None:
    """DIR-02: a dashboard or alert rule on the metric can tell simulated money from real."""
    from cosmic.ops.metrics import collect

    env = await build_env(settings, db)
    async with db.connect() as conn:
        metrics = await collect(conn, env.clock.now())
    line = next(x for x in metrics.splitlines() if x.startswith("cosmic_reconciliation_age"))
    assert 'account="paper-main"' in line and 'environment="PAPER"' in line


async def test_risk_policy_change_via_portal(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db)
    app = create_app(settings, db=db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = await c.get("/settings/risk")
        assert page.status_code == 200 and "max_daily_loss" in page.text
        env.clock.advance(minutes=11)  # step-up expired → refused
        r = await c.post(
            "/settings/risk",
            data={
                "csrf_token": csrf_from(page.text),
                "max_order_notional": "400",
                "reason": "tighten after review",
            },
        )
        assert r.status_code == 403
    async with db.connect() as conn:
        assert len(list(await conn.execute(select(risk_policies)))) == 1


async def test_every_form_posts_to_a_real_route(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    """UX-03: no decorative controls — every form action maps to a registered POST route."""
    app = create_app(settings, db=db, clock=clock)

    def flatten(routes: list[object]) -> list[object]:
        out: list[object] = []
        for r in routes:
            inner = getattr(r, "original_router", None)  # FastAPI ≥ 0.14x wraps included routers
            out += flatten(list(inner.routes)) if inner is not None else [r]
        return out

    posts = [
        r for r in flatten(list(app.routes)) if isinstance(r, APIRoute) and "POST" in r.methods
    ]
    patterns = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r.path) + "$") for r in posts]
    actions = set()
    for tpl in TEMPLATES.glob("*.html"):
        for action in re.findall(r'<form[^>]*method="post"[^>]*action="([^"]+)"', tpl.read_text()):
            actions.add(re.sub(r"\{\{[^}]+\}\}", "x", action))
    assert actions, "no forms found"
    missing = [a for a in actions if not any(p.match(a) for p in patterns)]
    assert not missing, missing
