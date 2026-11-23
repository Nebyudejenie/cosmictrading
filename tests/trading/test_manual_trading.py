"""Manual Trading (S113) end to end on the portal: the real account is shown as live only when
HFM itself reports a REAL account, the verified one, freshly; the calculator sizes on HFM's own
contract; the journal records once; the owner's rules change only with their code; the
portal's own BUY/SELL is locked and sends nothing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events
from cosmic.db.schema_execution import orders, trade_intents
from cosmic.db.schema_manual import manual_journal, manual_risk_settings
from cosmic.db.schema_trading import bridge_commands, bridge_state, broker_connections
from cosmic.manual import live
from cosmic.market import hfm_candles
from cosmic.trading.brokers.mt5_bridge import BRIDGE, BRIDGE_REAL, account_fingerprint
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user

pytestmark = pytest.mark.keeps_binance_paused

WEDNESDAY = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
SERVER = "HFMarketsGlobal-Live3"
REAL_FP = account_fingerprint(7000123, SERVER)
OFFSET = hfm_candles.server_offset_ms(WEDNESDAY)  # HFM's clock: New York + 7 h (UTC + 3 h)
GOLD = {
    "contract_size": 100.0,
    "volume_min": 0.01,
    "volume_step": 0.01,
    "volume_max": 50.0,
    "digits": 2,
    "tick_size": 0.01,
    "tick_value": 1.0,
    "stops_level": 0,
    "point": 0.01,
    "currency_profit": "USD",
    "currency_margin": "XAU",
    "swap_long": -50.0,
    "swap_short": 20.0,
}


def _ms(clock: FrozenClock, at: datetime | None = None) -> int:
    return int((at or clock.now()).timestamp() * 1000) + OFFSET


def _snapshot(clock: FrozenClock, **kw: Any) -> dict[str, Any]:
    equity = kw.get("equity", "1000.00")
    return {
        "bridge": kw.get("bridge", BRIDGE_REAL),
        "version": "13",
        "sent_at_ms": _ms(clock),
        "server_utc_offset_ms": kw.get("offset", OFFSET),
        "terminal": {
            "connected": True,
            "trade_allowed": True,
            "read_only": kw.get("read_only", True),
        },
        "account": {
            "fingerprint": kw.get("fingerprint", REAL_FP),
            "trade_mode": kw.get("trade_mode", "REAL"),
            "currency": "USD",
            "leverage": 1000,
            "balance": kw.get("balance", "1000.00"),
            "equity": equity,
            "margin": "0",
            "margin_free": equity,
            "server": SERVER,
            "company": "HF Markets",
            "trade_allowed": kw.get("trade_allowed", False),
            "margin_level": None,
            "positions_total": len(kw.get("positions", [])),
            "orders_total": 0,
        },
        "symbols": {"XAUUSD": GOLD},
        "ticks": {"XAUUSD": {"bid": 4000.0, "ask": 4000.35, "time_msc": _ms(clock) - 1000}},
        "positions": kw.get("positions", []),
        "orders": [],
        "history_orders": [],
        "deals": kw.get("deals", []),
    }


async def _report(db: Database, clock: FrozenClock, **kw: Any) -> None:
    snap = _snapshot(clock, **kw)
    at = kw.get("received_at", clock.now())
    async with db.transaction() as conn:
        await conn.execute(
            pg_insert(bridge_state)
            .values(bridge=snap["bridge"], received_at=at, snapshot=snap)
            .on_conflict_do_update(
                index_elements=[bridge_state.c.bridge],
                set_={"received_at": at, "snapshot": snap},
            )
        )


async def _link(db: Database, clock: FrozenClock) -> None:
    """The owner's real account, verified REAL on terminal 3 with its investor password."""
    now = clock.now()
    async with db.transaction() as conn:
        await conn.execute(
            broker_connections.insert().values(
                id=uuid.uuid4(),
                nickname="My real account",
                broker="HFM",
                platform="MT5",
                login=7000123,
                server=SERVER,
                fingerprint=REAL_FP,
                declared_type="REAL",
                password_kind="INVESTOR",
                slot=BRIDGE_REAL,
                status="CONNECTED",
                verified_type="REAL",
                verified_at=now,
                created_at=now,
                created_by="test",
                updated_at=now,
                updated_by="test",
            )
        )


async def _state(db: Database, clock: FrozenClock) -> live.LiveAccount:
    async with db.connect() as conn:
        return await live.read(conn, clock.now())


async def test_the_real_account_is_live_only_when_hfm_reports_it_real_verified_and_fresh(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    clock.set(WEDNESDAY)
    assert (await _state(db, clock)).state == live.NO_TERMINAL
    await _report(db, clock)
    assert (await _state(db, clock)).state == live.NOT_LINKED  # not verified in Connections
    await _link(db, clock)
    a = await _state(db, clock)
    assert (a.state, a.live, a.read_only, a.equity, a.priced) == (
        live.CONNECTED,
        True,
        True,
        1000,
        True,
    )
    assert a.spec is not None and a.spec.value_per_price == 100  # a lot moves 100 a dollar
    assert a.label == "LIVE-CONNECTED · READ-ONLY" and len(a.checks) == 10
    assert all(c.ok for c in a.checks), [c for c in a.checks if not c.ok]
    # The trading (master) password: HFM would accept orders on it, so it is never "connected".
    await _report(db, clock, trade_allowed=True)
    master = await _state(db, clock)
    assert (master.state, master.live, master.read_only) == (live.NOT_READ_ONLY, False, False)
    # A terminal that does not say it refuses trading commands (an older bridge): not connected.
    await _report(db, clock, read_only=False)
    assert (await _state(db, clock)).state == live.NOT_READ_ONLY
    # A bridge whose clock offset is not HFM's: the account shows, its price is never current.
    await _report(db, clock, offset=OFFSET - 15 * 60_000)
    skewed = await _state(db, clock)
    assert skewed.live and skewed.quote is not None and not skewed.quote.fresh
    assert not skewed.priced
    await _report(db, clock, trade_mode="DEMO")
    assert (await _state(db, clock)).state == live.NOT_LIVE  # never a demo shown as live
    await _report(db, clock, fingerprint="0" * 16)
    assert (await _state(db, clock)).state == live.NOT_LINKED  # another account
    await _report(db, clock, fingerprint="")
    assert (await _state(db, clock)).state == live.SIGNED_OUT
    await _report(db, clock, received_at=clock.now() - timedelta(seconds=61))
    stale = await _state(db, clock)
    assert stale.state == live.STALE and not stale.live and not stale.priced


async def test_without_a_live_account_the_page_says_so_and_previews_on_the_demo_contract(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    clock.set(WEDNESDAY)
    owner = await make_user(db, clock)
    await _report(db, clock, bridge=BRIDGE, trade_mode="DEMO", fingerprint="1" * 16)
    app = create_app(settings, db=db, clock=clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        page = (await c.get("/manual")).text
        assert "NOT CONNECTED" in page and "LIVE-CONNECTED" not in page
        assert "terminal 3 (the real account&#39;s read-only terminal) has never reported" in page
        assert "Connection checks (1 of 3 passed)" in page
        assert "PREVIEW · YOUR TYPED EQUITY · HFM DEMO CONTRACT" in page
        assert "XAUUSD contract: what is verified, and on which account" in page
        assert "measured from this account&#39;s own deals: none" in page  # the demo: no deals
        assert "the margin is only estimated (contract × price ÷ leverage)" in page
        assert "Stop-out level" in page and "read by bridge 13" in page
        assert "No verified results yet" in page  # a preview never reaches the results
        assert "0 OF 9 PASSED" in page
        assert "Probability" in page or "NO TRADE" in page
        assert "No economic-calendar source is connected" in page
        # 100 USD at 1 %: the smallest gold trade is too big for a 10.00 stop: refused.
        q = "side=BUY&entry=4000.35&stop=3990.35&target=4020.35&risk_pct=1"
        page = (await c.get(f"/manual?{q}")).text
        assert "NO TRADE" in page
        assert "HFM&#39;s smallest trade (0.01 lot) would lose 10.00 at this stop-loss" in page
        assert "fits from about 1,000 of equity" in page
        page = (await c.get(f"/manual?{q}&preview_equity=2000")).text
        assert "0.02" in page and "20.00" in page  # 1 % of 2,000: 0.02 lot
        assert "no live account" in page  # a preview is never ready to trade
        assert "a preview is never journalled as a trade" in page
        assert "Start Trade (locked)" in page


async def test_a_live_trade_is_sized_on_hfms_contract_and_journalled_once_with_its_result(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    clock.set(WEDNESDAY)
    owner = await make_user(db, clock)
    await _link(db, clock)
    await _report(db, clock)
    app = create_app(settings, db=db, clock=clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        q = "side=BUY&entry=4000.35&stop=3995.35&target=4010.35&risk_pct=1"
        page = (await c.get(f"/manual?{q}")).text
        assert "LIVE-CONNECTED · READ-ONLY" in page and "LIVE ACCOUNT DATA" in page
        assert "WITHIN YOUR RULES" in page, page[page.find("Risk checks") :][:1500]
        assert "Place it in MT5 yourself:</strong> BUY 0.02 lot XAUUSD" in page
        form = {
            "csrf_token": csrf_from(page),
            "request_key": "k" * 32,
            "kind": "PLACED",
            "side": "BUY",
            "entry": "4000.35",
            "stop": "3995.35",
            "target": "4010.35",
            "risk_pct": "1",
            "note": "both trends up",
        }
        r = await c.post("/manual/journal", data=form)
        assert r.status_code == 303 and r.headers["location"] == "/manual?recorded=placed"
        again = await c.post("/manual/journal", data=form)
        assert again.status_code == 409  # the same form twice: never recorded twice
        async with db.connect() as conn:
            (row,) = list(await conn.execute(select(manual_journal)))
        assert (row.kind, row.side, row.lots, row.risk_cash, row.reward_cash) == (
            "PLACED",
            "BUY",
            D("0.02"),
            10,
            20,
        )
        async with db.connect() as conn:  # append-only
            with pytest.raises(Exception, match=r"append-only|permission denied"):
                await conn.execute(update(manual_journal).values(note="changed"))
        # A second BUY right away is refused as a duplicate.
        page = (await c.get(f"/manual?{q}")).text
        assert "this looks like a duplicate" in page
        # HFM's deals of the trade, closed at its target: the journal shows the result.
        opened, closed = clock.now() + timedelta(seconds=20), clock.now() + timedelta(minutes=15)
        clock.set(closed + timedelta(minutes=1))
        deals = [
            {
                "ticket": 1,
                "order": 1,
                "time_msc": _ms(clock, opened),
                "type": 0,
                "entry": 0,
                "magic": 0,
                "position_id": 55,
                "reason": 0,
                "volume": 0.02,
                "price": 4000.35,
                "commission": 0.0,
                "swap": 0.0,
                "profit": 0.0,
                "fee": 0.0,
                "symbol": "XAUUSD",
            },
            {
                "ticket": 2,
                "order": 2,
                "time_msc": _ms(clock, closed),
                "type": 1,
                "entry": 1,
                "magic": 0,
                "position_id": 55,
                "reason": 5,
                "volume": 0.02,
                "price": 4010.35,
                "commission": 0.0,
                "swap": 0.0,
                "profit": 20.0,
                "fee": 0.0,
                "symbol": "XAUUSD",
            },
        ]
        await _report(db, clock, deals=deals, balance="1020.00", equity="1020.00")
        page = (await c.get("/manual")).text
        assert "+20.00</span> (#55)" in page
        assert "1 of 1 closed trades" in page
        assert "0.00 a lot, both sides" in page  # no commission paid
        assert "from 2 of HFM&#39;s own deals on this account only" in page
        # The real account's own figures, beside the demo's: its one closed trade was booked
        # by HFM at exactly (exit - entry) x lots x 100.
        assert "HFM&#39;s booked profit on 1 closed trade equals" in page
        assert "By month" in page and "Below its best now" in page


async def test_the_owners_rules_refuse_more_risk_and_change_only_with_their_code(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    clock.set(WEDNESDAY)
    owner = await make_user(db, clock)
    await _link(db, clock)
    await _report(db, clock)
    app = create_app(settings, db=db, clock=clock)
    async with client_for(app) as c:
        csrf = await full_login(c, owner, clock)
        q = "side=BUY&entry=4000.35&stop=3995.35&target=4010.35&risk_pct=3"
        page = (await c.get(f"/manual?{q}")).text
        assert "above your limit of 2 % a trade" in page
        rules = {
            "csrf_token": csrf,
            "risk_pct_default": "1",
            "risk_pct_max": "3",
            "reason": "my plan allows 3 %",
            "weekdays_only": "on",
        }
        r = await c.post("/manual/rules", data=rules)
        assert r.status_code == 303
        page = (await c.get(f"/manual?{q}")).text
        assert "above your limit" not in page and "0.06" in page  # 3 % of 1,000 at 5.00
        too_much = await c.post("/manual/rules", data={**rules, "risk_pct_max": "11"})
        assert too_much.status_code == 422 and "the rules are not valid" in too_much.text
        clock.advance(minutes=11)  # the code is no longer fresh: a new one is needed
        stale = await c.post("/manual/rules", data={**rules, "risk_pct_max": "2"})
        assert stale.status_code in (401, 403)
    async with db.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(manual_risk_settings)) == 1
        with pytest.raises(Exception, match=r"append-only|permission denied"):
            await conn.execute(text("DELETE FROM manual_risk_settings"))


async def test_the_portals_own_buy_and_sell_is_locked_and_sends_nothing(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    clock.set(WEDNESDAY)
    owner = await make_user(db, clock)
    await _link(db, clock)
    await _report(db, clock)
    app = create_app(settings, db=db, clock=clock)
    async with client_for(app) as c:
        csrf = await full_login(c, owner, clock)
        r = await c.post(
            "/manual/execute",
            data={
                "csrf_token": csrf,
                "side": "BUY",
                "entry": "4000.35",
                "stop": "3995.35",
                "target": "4010.35",
                "risk_pct": "1",
            },
        )
        assert r.status_code == 409
        assert "Portal execution is locked — nothing was sent to HFM" in r.text
        assert "the HFM live connection is not built into this version" in r.text
        assert "the server allows at most PAPER_TRADING" in r.text
        assert "0 of 9 safety gates passed" in r.text
        assert "signed in read-only (its investor password)" in r.text
    async with db.connect() as conn:
        for table in (trade_intents, orders, bridge_commands):
            assert await conn.scalar(select(func.count()).select_from(table)) == 0
        refused = await conn.scalar(
            select(func.count())
            .select_from(audit_events)
            .where(audit_events.c.action == "manual.portal_execution_refused")
        )
    assert refused == 1
