"""The $100 rehearsal at its limits (audit 2026-11-03), with real paper fills; only the rule's
signal is forced. Losing trades walk it down to its floor of $80 and it stops for good there,
also after a renewal; the risk policy's defaults would freeze it first; a gap through a stop and
HFM's swap on a long hold can take a trade past its plan, and the owner is told; Binance's gold
is checked against HFM's gold before the test network's copy of Binance's book."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, incidents, notifications
from cosmic.db.schema_execution import (
    approval_packets,
    orders,
    paper_balances,
    post_trade_reviews,
    trade_intents,
    trading_mandates,
)
from cosmic.db.schema_market import fetch_runs, instruments, quotes, sources, venue_status, venues
from cosmic.notify.telegram.runner import notification_text
from cosmic.reporting import autotrader
from cosmic.research.library import auto, expect
from cosmic.research.library.catalog import BY_KEY
from cosmic.trading import mandates, rehearsal
from cosmic.trading.context_builder import account_state
from cosmic.trading.policy import DEFAULT_POLICY, change_policy
from tests.trading_env import Env, build_env

D = Decimal
PAXG = "SPOT:PAXG/USDT@BINANCE"
CENT = "CFD:XAU/USD@HFM_CENT"
RULE = "turtle_55_20"
GOLD_SWAP = {  # as the bridge reports XAUUSD (tests/trading/test_paper_cfd_swap.py)
    "swap_mode": 1,
    "swap_long": "-78.56",
    "swap_short": "0.0",
    "swap_rollover3days": 3,
    "point": "0.01",
    "contract_size": "100.0",
}


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    signal: dict[str, float] = {"last": 1.0, "in_session": 0.0}

    def forced(market: Any, params: Any) -> list[float]:  # flat until the last bar
        if signal.get("in_session"):  # until gold's last bar in session (HFM's weekend)
            k = max(i for i, d in enumerate(market.dates) if not expect.weekend_closed_at(d))
            return [0.0] * k + [signal["last"]] * (len(market.close) - k)
        return [0.0] * (len(market.close) - 1) + [signal["last"]]

    monkeypatch.setitem(BY_KEY, RULE, replace(BY_KEY[RULE], fn=forced))
    e = await build_env(settings, db)
    e.extra["signal"] = signal
    async with e.db.transaction() as conn:  # as Binance's exchangeInfo and the bridge state them
        for key, lot, notional in ((PAXG, "0.0001", "5"), (CENT, "0.01", None)):
            await conn.execute(
                update(instruments)
                .where(instruments.c.canonical_key == key)
                .values(
                    status="ONLINE",
                    tick_size=D("0.01"),
                    lot_size=D(lot),
                    min_qty=D(lot),
                    min_notional=D(notional) if notional else None,
                )
            )
    return e


async def ready_policy(
    env: Env, *, cent: bool = False, advised: bool = True, streak: int | None = None
) -> None:
    """What "Get them ready" applies: the fees, the markets, and (ticked) the advised limits."""
    base = DEFAULT_POLICY.model_dump(mode="python")
    fees: list[dict[str, object]] = [{"venue": "BINANCE", "bps": D(10)}]
    params = dict(base)
    if cent:
        fees.append({"venue": "HFM_CENT", "bps": D(0)})
        params |= {
            "allowed_instruments": (*base["allowed_instruments"], CENT),
            "allowed_venues": (*base["allowed_venues"], "HFM_CENT"),
        }
    params["venue_fees"] = tuple(fees)
    if advised:
        params |= autotrader.policy_advice(DEFAULT_POLICY, rehearsal.START, D(2))
    if streak is not None:
        params["max_loss_streak"] = streak
    async with env.db.transaction() as conn:
        await change_policy(
            conn,
            principal=env.principal(),
            new_params=params,
            reason="audit: ready",
            now=env.clock.now(),
            settings=env.settings,
        )


async def start(env: Env, venue: str = "binance", *, renew: bool = False) -> None:
    async with env.db.transaction() as conn:
        await rehearsal.start(
            conn,
            principal=env.principal(),
            broker=env.svc.paper,
            rule=RULE,
            now=env.clock.now(),
            settings=env.settings,
            venue=venue,
            auto_renew=renew,
        )
    await env.svc.reconcile_all(trigger="test")


async def daily_bars(env: Env, days: int = 420, half_range: int = 24) -> None:
    """Completed PAXG daily bars up to yesterday: 4000 ± 24, so ATR(20) is 48 (1.2 %, gold's)."""
    last = mandates.day_start(env.clock.now()) - timedelta(days=1)
    now = env.clock.now()
    async with env.db.transaction() as conn:
        inst = await conn.scalar(
            select(instruments.c.id).where(instruments.c.canonical_key == PAXG)
        )
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == "binance_public"))
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run, source_id=src, kind="t", started_at=now, finished_at=now, status="SUCCESS"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, low, "
                "close, volume, is_complete, fetch_run_id, ingested_at, updated_at) VALUES (:i, :s, "
                "'1d', :t, 4000, :h, :l, 4000, 1, true, :r, :n, :n) ON CONFLICT DO NOTHING"
            ),
            [
                {
                    "i": inst,
                    "s": src,
                    "t": last - timedelta(days=k),
                    "h": 4000 + half_range,
                    "l": 4000 - half_range,
                    "r": run,
                    "n": now,
                }
                for k in range(days)
            ],
        )


TESTNET = "SPOT:PAXG/USDT@BINANCE_TESTNET"
CENTS = D("0.01")
SOURCE = {
    PAXG: "binance_public",
    CENT: "hfm_cent_terms",
    TESTNET: "binance_testnet_public",
    "SPOT:PAXG/USD@COINBASE": "coinbase_exchange_public",
    "SPOT:PAXG/USD@BITSTAMP": "bitstamp_public",
    "CFD:XAU/USD@HFM": "hfm_mt5_bridge",
}


async def quote(
    env: Env,
    bid: str,
    ask: str,
    key: str = PAXG,
    *,
    testnet: bool = True,
    independent: bool = False,
) -> None:
    """A fresh quote, as production stores them: for PAXG also the test network's copy of
    Binance's book; PAXG/USD on Coinbase and Bitstamp only with ``independent`` (production
    retired them, ADR-027); for HFM Cent also HFM's own quote."""
    await one_quote(env, key, bid, ask)
    if key == PAXG and testnet:
        await one_quote(env, TESTNET, bid, ask)
    if key == PAXG and independent:  # PAXG/USD about 18 bps below, as on 2026-11-03
        usd = D("0.9982")
        for k in ("SPOT:PAXG/USD@COINBASE", "SPOT:PAXG/USD@BITSTAMP"):
            await one_quote(
                env, k, str((D(bid) * usd).quantize(CENTS)), str((D(ask) * usd).quantize(CENTS))
            )
    if key == CENT:  # the bridge stores HFM's own quote and its Cent mirror together
        await one_quote(env, "CFD:XAU/USD@HFM", bid, ask)


async def one_quote(env: Env, key: str, bid: str, ask: str) -> None:
    now = env.clock.now()
    async with env.db.transaction() as conn:
        inst = await conn.scalar(select(instruments.c.id).where(instruments.c.canonical_key == key))
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == SOURCE[key]))
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run, source_id=src, kind="t", started_at=now, finished_at=now, status="SUCCESS"
            )
        )
        await conn.execute(
            pg_insert(quotes)
            .values(
                id=uuid7(),
                instrument_id=inst,
                source_id=src,
                bid=D(bid),
                ask=D(ask),
                last=D(bid),
                source_ts=now,
                ingested_at=now,
                fetch_run_id=run,
            )
            .on_conflict_do_nothing()
        )


async def next_morning(env: Env) -> None:
    """00:40 UTC the next day (03:40 in Addis Ababa): yesterday's bar is complete."""
    env.clock.set(mandates.day_start(env.clock.now()) + timedelta(days=1, minutes=40))
    await worker_feeds(env)


async def worker_feeds(env: Env) -> None:
    """What the worker keeps fresh: each venue's status and every account's reconciliation."""
    now = env.clock.now()
    async with env.db.transaction() as conn:
        for venue, source in (("BINANCE", "binance_status"), ("HFM_CENT", "hfm_mt5_status")):
            vid = await conn.scalar(select(venues.c.id).where(venues.c.key == venue))
            sid = await conn.scalar(select(sources.c.id).where(sources.c.key == source))
            await conn.execute(
                venue_status.insert().values(
                    id=uuid7(),
                    venue_id=vid,
                    source_id=sid,
                    status="OPERATIONAL",
                    indicator="none",
                    description="audit",
                    source_ts=now,
                    ingested_at=now,
                )
            )
    await env.svc.reconcile_all(trigger="test")


async def money(env: Env, venue: str = "binance") -> tuple[Decimal, Decimal | None]:
    """(the paper venue's cash, the internal ledger's equity) of the rehearsal account."""
    async with env.db.connect() as conn:
        acct = await rehearsal.account(conn, venue)
        assert acct is not None
        cash = await conn.scalar(
            select(func.sum(paper_balances.c.total)).where(
                paper_balances.c.account_ref == acct.id,
                paper_balances.c.asset == rehearsal.REHEARSALS[venue].currency,
            )
        )
        state = await account_state(conn, acct, env.clock.now())
    return D(str(cash)), state.equity


async def last_trade(env: Env) -> dict[str, Any]:
    async with env.db.connect() as conn:
        row = (
            await conn.execute(
                select(post_trade_reviews.c.realized_pnl, post_trade_reviews.c.assessment)
                .order_by(post_trade_reviews.c.created_at.desc())
                .limit(1)
            )
        ).one()
    sizing = row.assessment["sizing"]
    return {
        "pnl": D(str(row.realized_pnl)),
        "planned": D(str(sizing["planned_max_loss"])),
        "ratio": sizing["loss_vs_planned_max"],
    }


async def stop_of_open_trade(env: Env) -> tuple[Decimal, Decimal]:
    async with env.db.connect() as conn:
        stop = (
            await conn.execute(
                select(orders.c.stop_price, orders.c.limit_price).where(
                    orders.c.purpose == "STOP_LOSS", orders.c.status.in_(("OPEN", "ACKNOWLEDGED"))
                )
            )
        ).first()
        if stop is None:
            stop = (
                await conn.execute(
                    select(orders.c.stop_price, orders.c.limit_price, orders.c.status)
                    .where(orders.c.purpose == "STOP_LOSS")
                    .order_by(orders.c.created_at.desc())
                    .limit(1)
                )
            ).one()
    return D(str(stop.stop_price)), D(str(stop.limit_price))


async def enter(env: Env, venue: str = "binance", half_range: int = 24) -> list[str]:
    key = PAXG if venue == "binance" else CENT
    await daily_bars(env, half_range=half_range)
    await quote(env, "4000.00", "4000.50", key)
    if key != PAXG:
        await quote(env, "4000.00", "4000.50", PAXG)
    out = await auto.run(env.db, env.clock)
    await env.cycle(3)
    return out


async def stopped_out(env: Env, *, gap: bool = False, venue: str = "binance") -> None:
    """The price falls to just above the stop's limit (a stop-out at the worst fill a stop-limit
    allows), just below a market stop (HFM Cent, S54), or with ``gap`` 50 below either."""
    key = PAXG if venue == "binance" else CENT
    stop, limit = await stop_of_open_trade(env)
    env.clock.advance(minutes=30)
    await worker_feeds(env)
    at_market = limit == stop
    bid = (limit - 50) if gap else (stop - CENTS if at_market else limit + CENTS)
    await quote(env, str(bid), str(bid + D("0.50")), key)
    await env.cycle(3)


async def skips(env: Env) -> list[str]:
    async with env.db.connect() as conn:
        return [
            str(r[0])
            for r in await conn.execute(
                select(notifications.c.payload["reason"].astext)
                .where(notifications.c.kind == "auto_trade_skipped")
                .order_by(notifications.c.created_at)
            )
        ]


async def mandate(env: Env) -> Any:
    async with env.db.connect() as conn:
        return (
            await conn.execute(
                select(trading_mandates).order_by(trading_mandates.c.granted_at.desc()).limit(1)
            )
        ).one()


# ---- down to the floor ------------------------------------------------


async def test_losing_trades_walk_down_to_the_floor_and_it_stops_for_good(env: Env) -> None:
    await ready_policy(env)
    await start(env)
    m = await mandate(env)
    assert (m.capital, m.max_loss_per_trade, m.total_loss_limit, m.daily_loss_limit) == (
        D(100),
        D("2.00"),
        D("20.00"),
        D("6.00"),
    )
    assert await money(env) == (D(100), D(100))
    planned = []
    for day in range(1, 25):
        await next_morning(env)
        out = await enter(env)
        if not out:
            break
        assert out == [f"{PAXG}:APPROVED"], (day, out, await skips(env))
        await stopped_out(env)
        t = await last_trade(env)
        planned.append(t["planned"])
        assert -t["pnl"] <= t["planned"] <= D("2.00"), t  # never more than its planned loss
    m = await mandate(env)
    cash, equity = await money(env)
    assert m.status == "STOPPED" and "one more full loss (2.00)" in m.end_reason
    assert D(80) <= cash < D(82) and equity is not None and equity >= D(80)
    assert planned[-1] <= D("1.00")  # half size after a deep drop (addendum 4)
    assert cash == equity  # the paper venue and the internal ledger agree
    # Nothing trades after the stop for good, and a start is refused.
    await next_morning(env)
    assert await enter(env) == []
    with pytest.raises(Exception, match="not restarted"):
        await start(env)


async def test_without_the_advised_limits_the_policy_freezes_it_first(env: Env) -> None:
    """Defaults (drawdown 10 %, four losses in a row) stop new trades long before the floor."""
    await ready_policy(env, advised=False)
    await start(env)
    seen = []
    for _ in range(1, 12):
        await next_morning(env)
        out = await enter(env)
        seen.append(out)
        if out != [f"{PAXG}:APPROVED"]:
            break
        await stopped_out(env)
    cash, _ = await money(env)
    assert len(seen) == 5 and seen[-1] == [f"{PAXG}:NON_EXECUTABLE"] and cash > D(90)
    assert "R08: 4 consecutive losing trades (limit 4)" in (await skips(env))[-1]


# ---- the cross-venue check of the Binance rehearsal ---------------------------------------------


async def test_the_binance_cross_check_uses_hfm_gold_before_the_test_network(env: Env) -> None:
    """Fixed by the audit: Binance's gold is compared with HFM's gold when HFM's price is fresh,
    and only otherwise (HFM's weekend) with the test network's copy of Binance's book."""
    await ready_policy(env)
    await start(env)
    hfm = "CFD:XAU/USD@HFM"
    # The test network has no fresh price (down, or reset); HFM's gold is fresh.
    await next_morning(env)
    await daily_bars(env)
    await quote(env, "4000.00", "4000.50", testnet=False)
    await one_quote(env, hfm, "4001.00", "4001.40")
    assert await auto.run(env.db, env.clock) == [f"{PAXG}:APPROVED"]
    await env.cycle(3)
    await stopped_out(env)
    # The test network's book 80 bps away: it no longer matters.
    await next_morning(env)
    await daily_bars(env)
    await quote(env, "4000.00", "4000.50", testnet=False)
    await one_quote(env, TESTNET, "4032.00", "4032.50")
    await one_quote(env, hfm, "4001.00", "4001.40")
    assert await auto.run(env.db, env.clock) == [f"{PAXG}:APPROVED"]
    await env.cycle(3)
    await stopped_out(env)
    # Binance's price 80 bps from HFM's is refused, though the test network copies it.
    await next_morning(env)
    await daily_bars(env)
    await quote(env, "4000.00", "4000.50")
    await one_quote(env, hfm, "4032.00", "4032.40")
    assert await auto.run(env.db, env.clock) == [f"{PAXG}:NON_EXECUTABLE"]
    assert "venues differ" in (await skips(env))[-1]
    # HFM silent (its weekend): the test network's copy is the last resort, as before.
    await next_morning(env)
    await daily_bars(env)
    await quote(env, "4000.00", "4000.50")
    assert await auto.run(env.db, env.clock) == [f"{PAXG}:APPROVED"]


# ---- a gap through the stop-limit ------------------------------------------------------------


async def test_a_gap_through_the_stop_is_not_silent_but_can_pass_the_plan(env: Env) -> None:
    await ready_policy(env)
    await start(env)
    await next_morning(env)
    assert await enter(env) == [f"{PAXG}:APPROVED"]
    await stopped_out(env, gap=True)
    async with env.db.connect() as conn:
        held = await conn.scalar(text("SELECT count(*) FROM positions WHERE quantity > 0"))
        inc = list(await conn.execute(select(incidents.c.dedup_key, incidents.c.severity)))
        asked = list(
            await conn.execute(
                select(trade_intents.c.status, trade_intents.c.created_by_kind).where(
                    trade_intents.c.side == "SELL"
                )
            )
        )
        kinds = [r[0] for r in await conn.execute(select(notifications.c.kind))]
        packet = await conn.scalar(select(approval_packets.c.packet).limit(1))
    _, equity = await money(env)
    assert held == 1  # the stop-limit could not fill
    assert [i[0].startswith("trading.stop_gapped:") for i in inc] == [True]
    assert asked == [("PROPOSED", "WORKER")] and "approval_requested" in kinds  # the owner's exit
    planned = D(str(packet["risk"]["estimated_max_loss"]))
    assert equity is not None and D(100) - equity > planned  # already past its plan
    # The rule keeps the trade while the owner decides: no new entry, no stop for good.
    await next_morning(env)
    assert await enter(env) == []


# ---- HFM's swap on a long hold ----------------------------------------------------------------


async def test_hfm_swap_on_a_long_hold_passes_the_planned_loss(env: Env) -> None:
    await ready_policy(env, cent=True)
    async with env.db.transaction() as conn:
        await conn.execute(
            update(instruments)
            .where(instruments.c.canonical_key == CENT)
            .values(metadata=GOLD_SWAP)
        )
    env.clock.set(datetime.fromisoformat("2026-10-19T00:10:00+00:00"))  # a Monday
    await worker_feeds(env)
    await start(env, "hfm-cent")
    await next_morning(env)  # Tuesday 00:40 UTC
    assert await enter(env, "hfm-cent") == [f"{CENT}:APPROVED"]
    env.clock.advance(days=84)  # twelve weeks later, still inside the 90-day run
    await stopped_out(env, venue="hfm-cent")
    t = await last_trade(env)
    cash, equity = await money(env, "hfm-cent")
    assert cash == equity == D(100) + t["pnl"]  # the swap is booked in both records
    async with env.db.connect() as conn:
        closed = list(
            await conn.execute(
                select(notifications.c.payload).where(notifications.c.kind == "trade_closed")
            )
        )
    assert -t["pnl"] > t["planned"], "the swap takes the loss past the plan"
    payload = closed[-1][0]
    assert D(payload["over_plan"]) == -t["pnl"] - t["planned"]
    text_ = notification_text("trade_closed", payload)
    assert f"more than the {t['planned']:,.2f} planned at its stop" in text_


# ---- renewal ----------------------------------------------------------------------------------


async def test_renewal_after_losses_keeps_the_80_floor_and_is_audited(env: Env) -> None:
    await ready_policy(env)
    await start(env, renew=True)
    for _ in range(3):
        await next_morning(env)
        assert await enter(env) == [f"{PAXG}:APPROVED"]
        await stopped_out(env)
    first = await mandate(env)
    cash, _ = await money(env)
    env.clock.set(first.expires_at + timedelta(minutes=5))
    await worker_feeds(env)
    await quote(env, "4000.00", "4000.50")
    await auto.run(env.db, env.clock)  # expires the run, then renews it
    second = await mandate(env)
    assert second.id != first.id and second.account_id == first.account_id
    assert second.capital == cash.quantize(D("0.01"))
    assert second.capital - second.total_loss_limit == D(80)  # the floor stays at 80
    async with env.db.connect() as conn:
        accounts = await conn.scalar(
            text("SELECT count(*) FROM accounts WHERE alias LIKE 'rehearsal-%'")
        )
        audited = [
            r[0]
            for r in await conn.execute(
                select(audit_events.c.action).where(audit_events.c.action.like("mandate.%"))
            )
        ]
    assert accounts == 1 and "mandate.renewed" in audited and "mandate.expired" in audited
    # The second run walks on down to the same floor.
    for _ in range(20):
        await next_morning(env)
        out = await enter(env)
        if not out:
            break
        await stopped_out(env)
    final = await mandate(env)
    cash, equity = await money(env)
    assert final.status == "STOPPED" and D(80) <= cash == equity
    # A stopped rehearsal never renews: its expiry passes and nothing new is granted.
    env.clock.set(final.expires_at + timedelta(days=1))
    await quote(env, "4000.00", "4000.50")
    await auto.run(env.db, env.clock)
    assert (await mandate(env)).id == final.id


# ---- the owner's choices after the audit (S54) ------------------------------------------------


async def hfm_cent(env: Env, *, streak: int | None = None) -> None:
    await ready_policy(env, cent=True, streak=streak)
    async with env.db.transaction() as conn:
        await conn.execute(
            update(instruments)
            .where(instruments.c.canonical_key == CENT)
            .values(metadata=GOLD_SWAP)
        )
    env.clock.set(datetime.fromisoformat("2026-10-19T00:10:00+00:00"))  # a Monday
    await worker_feeds(env)
    await start(env, "hfm-cent")


async def test_only_a_rehearsal_keeps_swap_aside(env: Env) -> None:
    """The owner chose the reserve for the $100 rehearsals: another account's auto-trader on a
    swap market keeps nothing aside (found by review)."""
    await hfm_cent(env)
    await quote(env, "4000.00", "4000.50", CENT)
    async with env.db.connect() as conn:
        m = mandates._mandate(await mandate(env))
        kept = await mandates.swap_reserve(conn, m, env.clock.now())
        other = await mandates.swap_reserve(
            conn, replace(m, account_id=env.account_id), env.clock.now()
        )
    assert kept > 0 and other == 0


async def test_hfm_cent_sells_a_gapped_stop_at_market_as_hfm_does(env: Env) -> None:
    await hfm_cent(env)
    await next_morning(env)
    assert await enter(env, "hfm-cent") == [f"{CENT}:APPROVED"]
    stop, limit = await stop_of_open_trade(env)
    assert limit == stop  # placed at the stop, not 0.5 % below
    await stopped_out(env, gap=True, venue="hfm-cent")
    t = await last_trade(env)
    async with env.db.connect() as conn:
        held = await conn.scalar(text("SELECT count(*) FROM positions WHERE quantity > 0"))
        gaps = await conn.scalar(select(func.count()).select_from(incidents))
        fill = await conn.scalar(
            select(func.min(orders.c.avg_fill_price)).where(orders.c.purpose == "STOP_LOSS")
        )
    assert held == 0 and gaps == 0  # sold at once: nothing left waiting for the owner
    assert fill is not None and fill < stop - 49  # at the gapped price, as MT5 fills its sl
    assert -t["pnl"] > t["planned"]  # past its plan, and the owner is told so


async def test_binance_keeps_the_stop_limit_of_a_real_binance_account(env: Env) -> None:
    await ready_policy(env)
    await start(env)
    await next_morning(env)
    assert await enter(env) == [f"{PAXG}:APPROVED"]
    stop, limit = await stop_of_open_trade(env)
    assert limit < stop  # a stop-limit 0.5 % below, as Binance's STOP_LOSS_LIMIT


async def test_hfm_cent_keeps_its_swap_aside_and_ends_at_or_above_80(env: Env) -> None:
    """The stop for good keeps aside what one more full-size trade could pay in swap over 90
    nights: each trade held overnight pays its swap, and the run stops for good while the swap
    kept aside still fits above 80. Quiet bars (ATR 30) let the smallest Cent trade fit at half
    size too."""
    env.extra["signal"]["in_session"] = 1.0
    await hfm_cent(env, streak=30)  # small Cent losses: the floor, not the streak, decides here
    await quote(env, "4000.00", "4000.50", CENT)
    async with env.db.connect() as conn:
        m = mandates._mandate(await mandate(env))
        kept = await mandates.swap_reserve(conn, m, env.clock.now())
    assert D("0.80") < kept < D("1.00")  # 50 (half the money) x 0.0196 % a night x 90
    for _ in range(60):
        await next_morning(env)
        if env.clock.now().weekday() >= 5:
            continue  # gold is closed: the signal waits for Monday
        out = await enter(env, "hfm-cent", half_range=15)
        if not out:
            break
        assert out == [f"{CENT}:APPROVED"], (out, await skips(env))
        env.clock.advance(hours=18)  # held over the 17:00 New York rollover: it pays swap
        await stopped_out(env, venue="hfm-cent")
    final = await mandate(env)
    cash, equity = await money(env, "hfm-cent")
    async with env.db.connect() as conn:
        u = await mandates.usage(conn, mandates._mandate(final), env.clock.now())
        kept = await mandates.swap_reserve(conn, mandates._mandate(final), env.clock.now())
    lost = -u.realized_total
    assert final.status == "STOPPED", (final.status, await skips(env))
    assert f"with the overnight swap it could pay ({kept:,.2f})" in final.end_reason
    assert lost + final.max_loss_per_trade + kept > final.total_loss_limit  # why it stopped
    assert D(80) + kept <= cash + final.max_loss_per_trade  # a full last loss with its swap fits
    assert D(80) <= cash == equity


async def test_a_daily_entry_refused_for_missing_data_tries_again_within_the_day(
    env: Env,
) -> None:
    await ready_policy(env)
    await start(env)
    await next_morning(env)
    await daily_bars(env)
    await quote(env, "4000.00", "4000.50", testnet=False)  # no second price anywhere
    assert await auto.run(env.db, env.clock) == [f"{PAXG}:NON_EXECUTABLE"]
    assert (await skips(env))[-1].endswith("it tries again each hour while today's signal holds")
    assert await auto.run(env.db, env.clock) == []  # the same hour: not again
    env.clock.advance(hours=1)
    await worker_feeds(env)
    await quote(env, "4000.00", "4000.50", testnet=False)
    assert await auto.run(env.db, env.clock) == [f"{PAXG}:NON_EXECUTABLE"]  # still missing
    env.clock.advance(hours=1)
    await worker_feeds(env)
    await quote(env, "4000.00", "4000.50")  # the test network is back
    assert await auto.run(env.db, env.clock) == [f"{PAXG}:APPROVED"]
    env.clock.advance(hours=1)
    await worker_feeds(env)
    await quote(env, "4000.00", "4000.50")
    assert await auto.run(env.db, env.clock) == []  # bought once
    assert len(await skips(env)) == 1  # told once
    async with env.db.connect() as conn:
        buys = await conn.scalar(
            select(func.count())
            .select_from(trade_intents)
            .where(trade_intents.c.side == "BUY", trade_intents.c.status != "NON_EXECUTABLE")
        )
    assert buys == 1


async def test_a_refusal_that_is_not_missing_data_is_not_tried_again(env: Env) -> None:
    await ready_policy(env)
    await start(env)
    await next_morning(env)
    await daily_bars(env)
    await quote(env, "4000.00", "4000.50")
    await one_quote(env, "CFD:XAU/USD@HFM", "4032.00", "4032.40")  # 80 bps away: a real FAIL
    assert await auto.run(env.db, env.clock) == [f"{PAXG}:NON_EXECUTABLE"]
    assert "venues differ" in (await skips(env))[-1] and "tries again" not in (await skips(env))[-1]
    env.clock.advance(hours=1)
    await worker_feeds(env)
    await quote(env, "4000.00", "4000.50")
    await one_quote(env, "CFD:XAU/USD@HFM", "4001.00", "4001.40")
    assert await auto.run(env.db, env.clock) == []  # the day's signal was decided
