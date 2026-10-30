"""Paper fills against the venue's real order book (S41): Binance's public depth read, what a
size would cost through the levels, each paper fill on Binance prices compared once, and gate
G's fill comparison. The book reads go to stand-ins; nothing touches the network."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_execution import fill_book_checks
from cosmic.market import data as md
from cosmic.sources.connectors.binance import BookDepth, parse_depth
from cosmic.sources.contract import SourceSchemaError, SourceUnavailableError
from cosmic.trading import golive, real_book
from cosmic.trading.accounts import get_account
from cosmic.trading.exits import propose_close
from cosmic.trading.real_book import BookSummary, book_price
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.trading.test_binance_paper import BTC, binance_env

D = Decimal
AT = datetime(2026, 10, 30, tzinfo=UTC)


class Book:
    """A stand-in for Binance's public order book."""

    def __init__(self, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> None:
        self.depth = BookDepth(
            "BTCUSDT",
            tuple((D(p), D(q)) for p, q in bids),
            tuple((D(p), D(q)) for p, q in asks),
        )
        self.reads: list[str] = []

    async def fetch_depth(self, source_symbol: str, limit: int = 20) -> BookDepth:
        self.reads.append(source_symbol)
        return self.depth


class Down:
    async def fetch_depth(self, source_symbol: str, limit: int = 20) -> BookDepth:
        raise SourceUnavailableError("connection error contacting data-api.binance.vision")


def test_the_depth_answer_is_read_exactly_and_refused_when_wrong() -> None:
    depth = parse_depth(
        "PAXGUSDT",
        {
            "lastUpdateId": 1,
            "bids": [["4100.10", "0.5"], ["4100.00", "2"]],
            "asks": [["4100.30", "1"]],
        },
    )
    assert depth.bids == ((D("4100.10"), D("0.5")), (D("4100.00"), D("2")))
    assert depth.asks == ((D("4100.30"), D("1")),)
    for bad, why in (
        ({"bids": [], "asks": "x"}, "not a list"),
        ({"asks": []}, "no bids or asks"),
        ({"bids": [["4100", "1"], ["4101", "1"]], "asks": []}, "not in price order"),
        ({"bids": [["4101", "1"]], "asks": [["4100", "1"]]}, "crossed"),
        ({"bids": [["0", "1"]], "asks": []}, "not usable"),
        ({"bids": [["4100"]], "asks": []}, "malformed"),
    ):
        with pytest.raises(SourceSchemaError, match=why):
            parse_depth("PAXGUSDT", bad)


def test_a_size_costs_what_it_takes_through_the_levels() -> None:
    asks = [(D("100.00"), D("1")), (D("100.10"), D("2")), (D("100.50"), D("5"))]
    assert book_price(asks, D("0.5")) == (D("100.00"), 1)
    price, used = book_price(asks, D("2"))  # 1 at 100.00 and 1 at 100.10
    assert (price, used) == (D("100.05"), 2)
    assert book_price(asks, D("9")) == (None, 3)  # the levels read hold only 8
    assert book_price(asks, D("0")) == (None, 0)


def test_positive_means_the_paper_fill_was_harsher_than_the_real_book() -> None:
    def check(side: str, paper: str, book: str) -> Decimal | None:
        return real_book.BookCheck("BINANCE", "ENTRY", side, D(paper), D(book), AT, AT).bps

    assert check("BUY", "100.05", "100.00") == D("5.00")  # paid more than the book
    assert check("BUY", "99.95", "100.00") == D("-5.00")  # kinder than the book
    assert check("SELL", "99.90", "100.00") == D("10.00")  # got less than the book
    assert check("SELL", "100.10", "100.00") == D("-10.00")


def test_gate_g_needs_enough_fills_neither_kinder_nor_much_harsher() -> None:
    def s(checks: int, median: str | None, *, thin: int = 0, missed: int = 0) -> BookSummary:
        m = None if median is None else D(median)
        return BookSummary("BINANCE", checks, thin, missed, m, m, m, None, None)

    for ok in (s(20, "-5.00"), s(20, "25.00"), s(20, "3.10"), s(20, "0", missed=4)):
        assert golive.gate_g_fills(ok)[0], ok
    for short, why in (
        (s(20, "-5.01"), "outside -5 to +25 bps"),  # the simulator kinder than the real venue
        (s(20, "25.01"), "outside -5 to +25 bps"),  # so harsh that it distorts every result
        (s(19, "0"), None),
        (s(20, "0", thin=1), "too thin"),
        (s(20, "0", missed=5), "not compared in time"),  # more than one in five
    ):
        met, reasons = golive.gate_g_fills(short)
        assert not met, short
        assert why is None or any(why in r for r in reasons), (why, reasons)
    met, reasons = golive.gate_g_fills(s(0, None))
    assert not met and "none yet" in reasons[0]
    assert "3/20, median +1.20 bps" in golive.gate_g_fills(s(3, "1.20"))[1][0]


async def test_each_paper_fill_on_binance_prices_is_compared_once(
    settings: Settings, db: Database
) -> None:
    env = await binance_env(settings, db)
    r = await env.propose(account_alias="paper-binance", instrument_key=BTC)
    await env.approve(r.intent_id)
    await env.cycle(2)  # a BUY of 0.004 filled at 83,000 on Binance's stored ask
    down = await real_book.check_new_fills(env.db, env.clock, Down())
    assert (down.compared, down.unreadable) == (0, ("BTCUSDT",))  # unreadable: none
    book = Book(
        bids=[("82990.00", "0.5")],
        asks=[("82995.00", "0.001"), ("83005.00", "0.01")],  # 0.004 takes two levels
    )
    assert (await real_book.check_new_fills(env.db, env.clock, book)).compared == 1
    assert book.reads == ["BTCUSDT"]
    assert (await real_book.check_new_fills(env.db, env.clock, book)).compared == 0  # once only
    async with env.db.connect() as conn:
        row = (await conn.execute(select(fill_book_checks))).one()
        summary = await real_book.summary(conn, now=env.clock.now())
        gates = {g.key: g for g in await golive.readiness(conn, env.clock.now())}
    # 0.001 × 82,995 + 0.003 × 83,005 = 332.01 for 0.004: 83,002.50 a unit
    assert (row.book_price, row.best_price, row.levels) == (D("83002.5"), D("82995.00"), 2)
    assert (summary.checks, summary.median_bps, summary.missed) == (1, D("-0.30"), 0)
    assert any("1/20, median -0.30 bps" in why for why in gates["G"].reasons)
    for statement in ("UPDATE fill_book_checks SET levels = 9", "DELETE FROM fill_book_checks"):
        with pytest.raises(Exception, match=r"append-only|permission denied"):
            async with env.db.transaction() as conn:
                await conn.execute(text(statement))
    # A second fill (the exit), left uncompared: pending for a minute, then missed for good.
    async with env.db.transaction() as conn:
        account = await get_account(conn, alias="paper-binance")
        inst = await md.instrument_by_key(conn, BTC)
        sale = await propose_close(
            conn,
            account=account,
            inst=inst,
            reason="take the money off the table",
            actor=Actor.user(env.owner.user_id),
            now=env.clock.now(),
        )
    await env.approve(sale.intent_id)
    await env.cycle(3)  # the stop is cancelled, then the IOC sale fills on the stored bid
    async with env.db.connect() as conn:
        waiting = await real_book.pending(conn, env.clock.now())
        assert [p.side for p in waiting] == ["SELL"]
        assert (await real_book.summary(conn, now=env.clock.now())).missed == 0  # its minute
    env.clock.advance(seconds=61)
    async with env.db.connect() as conn:
        assert await real_book.pending(conn, env.clock.now()) == []  # too late to compare
        late = await real_book.summary(conn, now=env.clock.now())
        total = await conn.scalar(select(func.count()).select_from(fill_book_checks))
    assert (late.missed, total) == (1, 1)
    async with env.db.transaction() as conn:  # a book read past its minute is never kept
        assert await real_book.record(conn, waiting[0], book.depth, env.clock.now()) is None
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/settings/risk")).text
    assert "Paper fills against Binance&#39;s real order book" in page or (
        "Paper fills against Binance's real order book" in page
    )
    assert "-0.30 bps" in page and "1 of 20 fills compared" in page


async def test_the_worker_compares_through_the_market_connector_and_warns_when_it_cannot(
    settings: Settings, db: Database
) -> None:
    """The book check is the worker's own job, on the market-data connector (its client and
    rate limit): three runs that cannot read the book while a fill waits raise a warning, and
    the next good read resolves it."""
    import httpx

    from cosmic.db.schema import incidents
    from cosmic.market.jobs import build_ingest_service
    from cosmic.trading.jobs import check_real_book
    from cosmic.worker.context import WorkerContext

    env = await binance_env(settings, db)
    r = await env.propose(account_alias="paper-binance", instrument_key=BTC)
    await env.approve(r.intent_id)
    await env.cycle(2)
    up = {"now": False}

    def venue(request: httpx.Request) -> httpx.Response:
        if not up["now"]:
            return httpx.Response(503, text="Service Unavailable")
        assert request.url.path.endswith("/api/v3/depth")
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(
            200, json={"lastUpdateId": 1, "bids": [["82990", "1"]], "asks": [["83001", "1"]]}
        )

    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="test")
    ctx.services["ingest"] = build_ingest_service(ctx, transport=httpx.MockTransport(venue))

    async def warning() -> str | None:
        async with env.db.connect() as conn:
            return await conn.scalar(
                select(incidents.c.status).where(incidents.c.dedup_key == "real_book.unreadable")
            )

    for _ in range(2):
        run = await check_real_book(ctx)
        assert run is not None and run.unreadable == ("BTCUSDT",)
    assert await warning() is None  # two misses: not yet
    await check_real_book(ctx)
    assert await warning() == "OPEN"
    up["now"] = True
    run = await check_real_book(ctx)
    assert run is not None and run.compared == 1
    assert await warning() == "RESOLVED"
    async with env.db.connect() as conn:
        bps = (await real_book.summary(conn, now=env.clock.now())).median_bps
    assert bps == D("-0.12")  # 83,000 paid against 83,001 in the book


async def test_readiness_holds_gate_g_on_the_fill_comparison_alone(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With G recorded PASSED and its paper period met, the fill comparison alone decides G;
    C, F and I stay BLOCKED, so live modes stay unreachable."""
    from cosmic.audit.service import Actor
    from cosmic.trading.authority import live_preconditions

    env = await binance_env(settings, db)
    monkeypatch.setattr(golive, "PAPER_MIN_CLOSED_TRADES", 0)
    monkeypatch.setattr(golive, "PAPER_MIN_DAYS", 0)
    async with env.db.transaction() as conn:
        await golive.record_gate(
            conn,
            gate="G",
            status="PASSED",
            evidence={"note": "paper period and fill comparison for the test"},
            actor=Actor.system("test"),
            now=env.clock.now(),
        )

    async def gates() -> dict[str, golive.GateStatus]:
        async with env.db.connect() as conn:
            return {g.key: g for g in await golive.readiness(conn, env.clock.now())}

    assert (await gates())["G"].status == "NOT_MET"  # no fill compared yet
    realistic = BookSummary("BINANCE", 20, 0, 0, D("2.50"), D("-1.00"), D("6.00"), None, None)

    async def compared(conn: Any, **kw: Any) -> BookSummary:
        return realistic

    monkeypatch.setattr(real_book, "summary", compared)
    g = await gates()
    assert g["G"].status == "PASSED"
    assert [g[k].status for k in "CFI"] == ["BLOCKED"] * 3
    async with env.db.connect() as conn:
        unmet = await live_preconditions(conn, env.clock.now(), live_venues=["BINANCE"])
    assert "go-live gate G has not passed" not in unmet
    assert {f"go-live gate {k} has not passed" for k in "CFI"} <= set(unmet)
    kinder = BookSummary("BINANCE", 20, 0, 0, D("-6.00"), D("-9.00"), D("1.00"), None, None)

    async def kind(conn: Any, **kw: Any) -> BookSummary:
        return kinder

    monkeypatch.setattr(real_book, "summary", kind)
    assert (await gates())["G"].status == "NOT_MET"
