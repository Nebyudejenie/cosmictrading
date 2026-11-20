"""The owner's short daily report and the Telegram health summary (directive Phases 8 and 10).

Every account is reported on its own line, in its own currency and environment (PAPER,
TESTNET, DEMO, LIVE): results are never added across accounts, so paper or test money is never
mixed with real money. Realised and unrealised P&L stay apart. A value the system cannot verify
is shown as UNKNOWN, never guessed. Plain text, no secrets, aliases only. What needs attention
follows the same rules as the portal's Overview (``command_center.attention_items``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.logs import get_logger
from cosmic.db.schema import incidents, jobs, notifications
from cosmic.db.schema_execution import post_trade_reviews, trade_intents, trading_mandates
from cosmic.db.schema_gold import (
    gold_hfm_decisions,
    gold_predictions,
    gold_sim_accounts,
    gold_sim_decisions,
)
from cosmic.db.schema_trading import accounts, bridge_state
from cosmic.gold import controls, hfm_demo, hfm_selftest, sim, trend_sim, trend_test
from cosmic.market import data as md
from cosmic.reporting.command_center import attention_items, connections, strategy_status
from cosmic.reporting.hfm_home import today_split
from cosmic.trading import ledger, mandates, queries
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.modes import environment_short

DAY = timedelta(hours=24)
KIND = "daily_report"
MAX_POSITIONS = 4  # per account, to keep the message short
SHOWN_ABOVE = ("pending", "worker")  # attention kinds the health lines already state


def signed(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), ROUND_HALF_EVEN):+,}"


def money(value: Decimal | None, places: str = "0.01") -> str:
    if value is None:
        return "UNKNOWN"
    return f"{value.quantize(Decimal(places), ROUND_HALF_EVEN):,}"


def _age(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def _ts(value: str | None, now: datetime) -> str:
    if not value:
        return "UNKNOWN"
    return f"{_age((now - datetime.fromisoformat(value)).total_seconds())} ago"


def backup_line(backups: dict[str, Any] | None, now: datetime) -> str:
    if not backups:
        return "Backups: not reported by the worker"
    if backups.get("error"):
        return "Backups: CANNOT BE CHECKED (the worker cannot read the backup directory)"
    b_new, drill = backups.get("newest"), backups.get("drill")
    return (
        "Backups: "
        + (
            "NONE FOUND"
            if not b_new
            else f"newest {_ts(b_new['created_at'], now)}"
            + (" (INCOMPLETE)" if b_new["problems"] else "")
        )
        + " · restore drill "
        + (
            "never recorded"
            if not drill
            else f"{'PASS' if drill['passed'] else 'FAILED'} {drill['drilled_at'][:10]}"
        )
    )


async def health_lines(conn: AsyncConnection, now: datetime) -> list[str]:
    """Mode, worker, incidents, failed jobs, the HFM bridge, backups, pending approvals and what
    needs attention: the /status answer."""
    banner = await queries.authority_banner(conn)
    links = await connections(conn, now)
    open_incidents = list(
        await conn.execute(select(incidents.c.severity).where(incidents.c.status != "RESOLVED"))
    )
    critical = sum(1 for i in open_incidents if i.severity == "CRITICAL")
    failed = await conn.scalar(
        select(func.count())
        .select_from(jobs)
        .where(jobs.c.status == "DEAD", jobs.c.finished_at >= now - DAY)
    )
    waiting = await queries.intent_list(conn, statuses=("PROPOSED",))
    live = "ENABLED" if banner["live_enabled"] else "DISABLED"
    worker = links["worker_age_s"]
    lines = [
        f"Mode {banner['mode']} · live orders {live}",
        "Worker: "
        + ("NO HEARTBEAT" if worker is None else f"beat {_age(worker)} ago")
        + f" · open incidents {len(open_incidents)} ({critical} critical)"
        + f" · failed jobs (24 h) {failed or 0}",
    ]
    for b in links["bridges"]:
        state = (
            f"report {_age(b['age_s'])} ago ({b['account_mode']})"
            if b["fresh"]
            else f"NOT REPORTING for {_age(b['age_s'])}: HFM orders wait"
        )
        lines.append(f"HFM bridge v{b['version']}: {state}")
    lines.append(backup_line(links["backups"], now))
    lines.append(f"Proposals waiting for you: {len(waiting)}")
    rest = [text for kind, text in await attention_items(conn, now) if kind not in SHOWN_ABOVE]
    if rest:
        lines.append("Needs attention: " + "; ".join(rest))
    return lines


async def _accounts(conn: AsyncConnection, now: datetime) -> list[str]:
    out: list[str] = []
    for v in await queries.portfolio(conn, now):
        a = v.account
        env = environment_short(a.kind, a.broker_adapter)
        ccy = a.base_currency
        out.append(
            f"• {a.alias} [{env}] {ccy}: equity {money(v.equity)} · cash {money(v.cash)} · "
            f"unrealised {money(v.unrealized)} · realised today {money(v.realized_today)} · "
            f"reconciliation {a.reconciliation_status}"
        )
        # The system's money is a part of the venue's balance (managed capital); the rest is
        # the owner's, kept apart. Said, so the venue's larger balance never looks like a
        # mismatch (the owner saw 20,000 here and 99,792 in the portal, 2026-11-04).
        unmanaged = await ledger.book_balance(conn, a.id, "UNMANAGED", ccy)
        if unmanaged and v.equity is not None:
            out.append(
                f"   that is the system's money; at the venue {money(v.equity + unmanaged)} in "
                f"all, of which {money(unmanaged)} is yours, not managed by the system"
            )
        hfm = mt5_bridge.BY_ADAPTER.get(a.broker_adapter or "")
        if hfm is not None:  # the same split as the dashboard's Today (S65), per bridge (S74)
            snap = await conn.scalar(
                select(bridge_state.c.snapshot).where(bridge_state.c.bridge == hfm.name)
            )
            system, own = today_split(snap or {}, now)
            if system is not None and own is not None:
                out.append(
                    f"   HFM's deals today (UTC), after costs: {signed(system + own)} — yours by "
                    f"hand {signed(own)}, the system's {signed(system)}"
                )
        used = [
            f"{x['name'].lower()} {money(x['value'])} of {money(x['limit'])}"
            + ("%" if x["unit"] == "%" else "")
            for x in v.limits
            if x["unit"] != "trades"
        ]
        if used:
            out.append("   limits: " + " · ".join(used))
        held = [p for p in v.positions if p["quantity"]]
        for p in held[:MAX_POSITIONS]:
            out.append(
                f"   {p['key']} {p['quantity'].normalize():f} · stop {p['protection']} · "
                f"unrealised {money(p['unrealized'])}"
            )
        if len(held) > MAX_POSITIONS:
            out.append(f"   … and {len(held) - MAX_POSITIONS} more on the portal")
        if v.unknown_orders:
            out.append(f"   {v.unknown_orders} order(s) in an UNKNOWN state: see Orders")
    return out or ["• no accounts"]


async def _closed(conn: AsyncConnection, now: datetime) -> list[str]:
    rows = list(
        await conn.execute(
            select(
                post_trade_reviews.c.realized_pnl,
                trade_intents.c.trade_code,
                accounts.c.alias,
                accounts.c.kind,
                accounts.c.broker_adapter,
                accounts.c.base_currency,
            )
            .join(trade_intents, trade_intents.c.id == post_trade_reviews.c.intent_id)
            .join(accounts, accounts.c.id == trade_intents.c.account_id)
            .where(post_trade_reviews.c.created_at >= now - DAY)
            .order_by(desc(post_trade_reviews.c.created_at))
            .limit(6)
        )
    )
    if not rows:
        return ["Closed in the last 24 h: none"]
    out = ["Closed in the last 24 h (realised P&L after fees):"]
    for r in rows:
        env = environment_short(r.kind, r.broker_adapter)
        out.append(
            f"   {r.trade_code} on {r.alias} [{env}]: {money(r.realized_pnl)} {r.base_currency}"
        )
    return out


def strategy_line(status: dict[str, int]) -> str:
    on = status["switched_on"]
    if status["qualified"] == 0:
        return f"Strategies: NO QUALIFIED STRATEGY · rules switched on: {on}"
    return f"Strategies: {status['qualified']} qualified · rules switched on: {on}"


log = get_logger(__name__)


async def briefing_lines(conn: AsyncConnection, now: datetime) -> list[str]:
    """The morning briefing: the auto-trader, the rule competition and gold, in a few lines.
    A part that cannot be built is left out with a note; it never stops the report."""
    from cosmic.reporting import autotrader, gold
    from cosmic.research.library import compete_store
    from cosmic.trading import measured
    from cosmic.trading.policy import active_policy

    lines = ["Auto-trader and research:"]
    try:
        run = await compete_store.latest_run(conn)
        for board in await autotrader.boards(conn, now):  # each auto-trader apart
            lines += [f"- {line}" for line in autotrader.telegram_lines(board, now)[:3]]
            rule = board.mandate.strategy_key if board.mandate else "turtle_20_10"
            lines.append(f"- {compete_store.verdict_line(run, rule, board.timeframe)}")
        leader = run.forward_leaders(1) if run is not None else []
        if leader:
            e = leader[0]
            tf = "hourly" if e["timeframe"] == "1h" else "daily"
            lines.append(
                f"- Forward race leader: {e['name']} ({tf}): {e['forward']['trades']} trades, "
                f"{e['forward']['total_r']:+.1f} R since {run.forward_from:%d %b}"  # type: ignore[union-attr]
            )
        g = await gold.build(conn, now)
        lines += [f"- {line}" for line in gold.telegram_lines(g)[:3]]
        _, policy = await active_policy(conn)
        for fee in (await measured.measured_fees(conn)).values():
            hint = measured.advice(fee, policy.fee_bps_for(fee.venue))
            if hint:
                lines.append(f"- {hint}")
        recent = await measured.fill_slips(conn, since=now - measured.RECENT)
        for q in measured.fill_quality(recent):
            hint = measured.slippage_advice(q)
            if hint:
                tag = f"[SIMULATED · {q.environment}] " if q.simulated else ""
                lines.append(f"- {tag}{hint}")
    except Exception as exc:  # the briefing is extra: the report goes out without it
        log.warning("briefing_failed", error=str(exc)[:200])
        lines.append("- (briefing not available this time)")
    return lines


async def daily_report(conn: AsyncConnection, now: datetime) -> str:
    opened = await conn.scalar(
        select(func.count()).select_from(incidents).where(incidents.c.opened_at >= now - DAY)
    )
    lines = [f"COSMICTRADING daily report · {now:%Y-%m-%d %H:%M} UTC"]
    lines += await health_lines(conn, now)
    lines.append(f"Incidents opened in the last 24 h: {opened or 0}")
    lines.append("Accounts (each in its own currency; never added together):")
    lines += await _accounts(conn, now)
    lines += await _closed(conn, now)
    lines += await robot_lines(conn, now)
    lines.append(strategy_line(await strategy_status(conn)))
    lines += await briefing_lines(conn, now)
    return "\n".join(lines)[:3900]  # Telegram's limit is 4096 characters


async def robot_lines(conn: AsyncConnection, now: datetime) -> list[str]:
    """The two gold robots' last 24 hours (S108; the owner's section 14: the day's trades, the
    win rate, why the target was not reached). Robot A on its own HFM demo (S110), or in its
    simulation while that runs; Robot B on the demo: its decisions, its best probability against
    the owner's bands, its practice orders."""
    out: list[str] = []
    on_demo = await trend_test.test_mandates(conn, now)
    for m in on_demo:
        out += await _robot_a_demo(conn, m, now)
    row = (
        await conn.execute(
            select(gold_sim_accounts)
            .where(gold_sim_accounts.c.rule == trend_sim.RULE)
            .order_by(desc(gold_sim_accounts.c.created_at))
            .limit(1)
        )
    ).first()
    if row is not None and (row.status == "RUNNING" or not on_demo):
        st = await sim.state(conn, row.id, await sim.price_now(conn, now), now)
        closed = [
            p
            for p in await sim.positions(conn, row.id, status="CLOSED", limit=100)
            if p.exit_at is not None and p.exit_at >= now - DAY and p.result is not None
        ]
        nets = [p.result + p.swap for p in closed if p.result is not None]
        won = sum(1 for x in nets if x > 0)
        held = await sim.positions(conn, row.id)
        decided = list(
            await conn.execute(
                select(gold_sim_decisions.c.action, gold_sim_decisions.c.reasons).where(
                    gold_sim_decisions.c.account_id == row.id,
                    gold_sim_decisions.c.at >= now - DAY,
                    gold_sim_decisions.c.decision.in_(("BUY", "SELL")),
                )
            )
        )
        opened = sum(1 for d in decided if d.action == "OPENED")
        record = await trend_sim.performance(conn, row.id)
        w = record.last50
        out.append(
            f"Robot A (TEST robot) [SIMULATED] {row.status}: balance {money(st.balance)}, equity "
            f"{money(st.equity)} USD (started at {money(row.starting_balance)})"
        )
        out.append(
            f"  last 24 h: {len(nets)} trade(s) closed"
            + (
                f", {won} won, {len(nets) - won} lost, net {signed(sum(nets, Decimal(0)))}"
                if nets
                else ""
            )
            + f"; {len(decided)} signal(s), {opened} opened"
            + (
                f"; open: {held[0].side} {held[0].ounces.normalize():f} oz from "
                f"{held[0].entry_price}"
                if held
                else "; nothing open"
            )
        )
        if w.trades:
            assert w.win_rate is not None
            out.append(
                f"  its record: {w.trades} trade(s), {w.win_rate * 100:.0f} % won, net "
                f"{signed(w.net)} USD; {record.mode}"
            )
    decisions = list(
        await conn.execute(
            select(gold_hfm_decisions.c.action, func.count())
            .join(trading_mandates, trading_mandates.c.id == gold_hfm_decisions.c.mandate_id)
            .where(
                gold_hfm_decisions.c.at >= now - DAY,
                trading_mandates.c.strategy_key == mandates.GOLD_M1,  # Robot B's own
            )
            .group_by(gold_hfm_decisions.c.action)
        )
    )
    if decisions:
        best = await conn.scalar(
            select(
                func.max(func.greatest(gold_predictions.c.p_buy, gold_predictions.c.p_sell))
            ).where(gold_predictions.c.at >= now - DAY)
        )
        sent = sum(n for a, n in decisions if a == "PROPOSED")
        out.append(
            f"Robot B (1-minute, HFM demo): {sum(n for _, n in decisions)} decisions; its best "
            + (f"chance {Decimal(best) * 100:.1f} %" if best is not None else "chance unknown")
            + " (your rules: 30 % asks you, 50 % trades by itself)"
            + (f"; {sent} order(s) sent" if sent else "; no order sent")
        )
    return out


async def _robot_a_demo(conn: AsyncConnection, m: mandates.Mandate, now: datetime) -> list[str]:
    """Robot A on its own HFM demo (S110): HFM's own balance and equity for that account, its
    signals of the last 24 hours and what it sent, its net results, and what is open."""
    account = await get_account(conn, account_id=m.account_id)
    report = await hfm_selftest._report(conn, account.id)
    acct = report.get("account") or {}
    inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
    held = await hfm_demo._held_entries(conn, account, inst.id, {str(m.id)})
    price = await sim.price_now(conn, now)
    ctl = await controls.get(conn, m.id)
    res = (
        await controls.results(conn, m, ctl, now, held, price.bid, price.ask)
        if ctl is not None and price is not None
        else None
    )
    decided = list(
        await conn.execute(
            select(gold_hfm_decisions.c.action).where(
                gold_hfm_decisions.c.mandate_id == m.id,
                gold_hfm_decisions.c.at >= now - DAY,
                gold_hfm_decisions.c.decision.in_(("BUY", "SELL")),
            )
        )
    )
    sent = sum(1 for d in decided if d.action == "PROPOSED")

    def usd(key: str) -> str:
        value = acct.get(key)
        return money(Decimal(str(value))) if value is not None else "unknown"

    status = ctl.status.replace("_", " ") if ctl is not None else "RUNNING"
    return [
        f"Robot A (HFM demo 2) [DEMO · real orders, virtual money] {status}: balance "
        f"{usd('balance')}, equity {usd('equity')} USD at HFM",
        f"  last 24 h: {len(decided)} signal(s), {sent} sent to HFM"
        + (f"; net today {signed(res.today)}, since START {signed(res.total)}" if res else "")
        + ("; open: 1 trade, its stop and target held by HFM" if held else "; nothing open"),
    ]


async def sent_today(conn: AsyncConnection, now: datetime) -> bool:
    n = await conn.scalar(
        select(func.count())
        .select_from(notifications)
        .where(
            notifications.c.kind == KIND,
            notifications.c.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
        )
    )
    return bool(n)


def report_payload(text: str) -> dict[str, Any]:
    return {"text": text}
