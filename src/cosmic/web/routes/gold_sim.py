"""HFM gold only (S61-S63): the analysis of HFM's candles, and My Gold Auto-Trader, the one
panel where the owner chooses an amount, a mode, automation and permission (the system
calculates the rest), confirms START, answers the trades it asks about, and switches it off."""

from __future__ import annotations

import uuid
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from cosmic.audit.service import record as audit
from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.db.schema_execution import trading_mandates
from cosmic.db.schema_trading import accounts
from cosmic.gold import analysis, board, daily, hfm_demo, hfm_selftest, sim, timing
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import require
from cosmic.trading import mandates
from cosmic.trading.accounts import get_account
from cosmic.trading.policy import active_policy, change_policy
from cosmic.web.deps import authorize, authorize_form, state
from cosmic.web.views import render

router = APIRouter()
DEFAULT = sim.defaults(Decimal(50))
SIMULATION, HFM_DEMO = "SIMULATION", "HFM_DEMO"
HFM_ACCOUNT = "hfm-demo"
TOO_SMALL = "WAIT — Your selected trading amount is too small"
HFM_DAYS = mandates.UNTIL_STOPPED_DAYS  # until the owner switches it off (S67)


def _dec(form: Any, name: str, default: Decimal | None, *, label: str) -> Decimal | None:
    raw = str(form.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValidationFailedError(f"{label}: write a number, for example 50 or 2.5") from None
    if not value.is_finite():
        raise ValidationFailedError(f"{label}: write a number")
    return value


def _settings(form: Any) -> sim.Settings:
    """The owner's amount and permission; every other setting is calculated from the amount
    (``sim.defaults``) unless they changed it under Advanced."""
    amount = _dec(form, "amount", None, label="Trading amount")
    if amount is None:
        raise ValidationFailedError("Trading amount: it is needed")
    if not amount > 0:
        raise ValidationFailedError("Trading amount: it must be more than 0")
    base = sim.defaults(amount)
    tp = str(form.get("take_profit_r") or "").strip().lower()
    take_profit = (
        base.take_profit_r
        if tp == ""
        else None
        if tp == "none"
        else _dec(form, "take_profit_r", None, label="Take-profit")
    )
    try:
        positions = int(str(form.get("max_positions") or base.max_positions))
    except ValueError:
        raise ValidationFailedError("Trades open at once: write a whole number") from None
    risk = _dec(form, "risk_pct", base.risk_pct, label="Risk per trade")
    assert risk is not None
    # Calculated: three trades' worth at the chosen risk (6 % of the amount at 2 %).
    three = max((amount * risk * 3 / 100).quantize(Decimal("0.01"), ROUND_DOWN), Decimal("0.01"))
    daily = _dec(form, "daily_loss", three, label="Most a day may lose")
    open_risk = _dec(form, "max_open_risk_pct", base.max_open_risk_pct, label="At risk at once")
    drop = _dec(form, "max_drawdown_pct", base.max_drawdown_pct, label="Largest drop")
    assert risk is not None and daily is not None and open_risk is not None and drop is not None
    permission = str(form.get("permission") or "AUTO")
    if permission not in ("AUTO", "ASK"):
        raise ValidationFailedError("Trading permission: choose Automatic or Ask me first")
    return sim.Settings(
        amount=amount,
        risk_pct=risk,
        daily_loss=daily,
        max_open_risk_pct=open_risk,
        max_drawdown_pct=drop,
        max_positions=positions,
        take_profit_r=take_profit,
        allow_sell=str(form.get("allow_sell") or "on") != "off",
        permission=permission,
    )


def _mode(form: Any) -> str:
    mode = str(form.get("mode") or SIMULATION)
    if mode not in (SIMULATION, HFM_DEMO):
        raise ValidationFailedError("Trading mode: choose Simulation or HFM Demo")
    return mode


def _hfm_terms(chosen: sim.Settings) -> mandates.MandateTerms:
    """The owner's choice as a mandate on the HFM demo: their amount, risk and limits, the gold
    reading on HFM's gold, Automatic or Ask me first. Their numbers are never changed: what the
    mandate cannot hold is refused with the reason."""
    if chosen.risk_pct > 5:
        raise ValidationFailedError(
            "Risk per trade: on the HFM demo it can be at most 5 % (the auto-trader's limit)"
        )
    try:
        return mandates.terms_from_money(
            account_alias=HFM_ACCOUNT,
            money=chosen.amount,
            care="NORMAL",
            mode="ASK" if chosen.permission == "ASK" else "AUTO",
            instruments=(hfm_demo.GOLD,),
            strategy_key=mandates.GOLD_READING,
            timeframe=mandates.HOURLY,
            days=HFM_DAYS,
            risk_pct=chosen.risk_pct,
            open_positions=min(chosen.max_positions, 5),
            daily_loss=chosen.daily_loss,
        )
    except ConflictError as exc:
        raise ValidationFailedError(str(exc)) from None


@router.get("/gold/analysis", include_in_schema=False)
async def gold_analysis(request: Request) -> Response:
    """HFM gold on M1, M5, M15, H1 and D1 now: trend, momentum, volatility, support and
    resistance, and BUY, SELL or WAIT with the reasons."""
    principal = await authorize(request, Capability.READ_MARKET_DATA)
    st = state(request)
    now = st.clock.now()
    async with st.db.connect() as conn:
        reading = await sim.reading_now(conn, now)
        price, feed = await sim.feed(conn, now)
    when = timing.timing(now, reading, price.spread if price else None, feed == sim.FEED_OK)
    return await render(
        request,
        "gold_analysis.html",
        principal,
        reading=reading,
        price=price,
        feed=sim.FEED_WORDS.get(feed),
        when=when,
        now=now,
        names=analysis.NAMES,
        enough=analysis.ENOUGH,
    )


@router.get("/gold/sim", include_in_schema=False)
async def gold_sim(request: Request) -> Response:
    """My Gold Auto-Trader: the internal simulation (Mode A), SIMULATED, never sent to HFM."""
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    async with st.db.connect() as conn:
        b = await board.build(conn, st.clock.now())
    return await render(
        request, "gold_sim.html", principal, b=b, default=b.settings or DEFAULT, message=None
    )


async def _owner(request: Request) -> tuple[Any, Any]:
    st = state(request)
    principal, form = await authorize_form(request, Capability.APPROVE_TRADE)
    require(principal, Capability.APPROVE_TRADE, now=st.clock.now(), settings=st.settings)
    return principal, form


@router.post("/gold/sim/review", include_in_schema=False)
async def gold_sim_review(request: Request) -> Response:
    """Before START: what the owner chose and everything the system calculated from it, on one
    page, with the START button (and their code). Nothing starts here."""
    principal, form = await authorize_form(request, Capability.READ_ACCOUNT)
    st = state(request)
    mode = _mode(form)
    chosen = _settings(form)
    wrong = sim.problems(chosen, chosen.amount)
    if wrong:
        raise ValidationFailedError(" ".join(wrong))
    on = str(form.get("automation") or "ON") != "OFF"
    if mode == HFM_DEMO:
        _hfm_terms(chosen)  # refused here, with the reason, rather than at START
        if not on:
            raise ValidationFailedError(
                "HFM Demo starts with automation ON: with it OFF nothing would run"
            )
    async with st.db.connect() as conn:
        b = await board.build(conn, st.clock.now(), amount=chosen.amount)
        hfm = (
            await board.hfm_preview(conn, chosen, *await board.start_view(conn, b.reading, b.price))
            if mode == HFM_DEMO
            else None
        )
    b.calc = board.calculate(chosen, sim.fresh_state(chosen.amount), b.reading, b.price)
    return await render(
        request,
        "gold_sim_confirm.html",
        principal,
        b=b,
        chosen=chosen,
        automation_on=on,
        mode=mode,
        hfm=hfm,
    )


@router.post("/gold/sim/start", include_in_schema=False)
async def gold_sim_start(request: Request) -> Response:
    """START, confirmed with the owner's code: a new simulation whose starting balance is their
    amount (or the balance they gave), with their settings. The one before stops, its records
    kept. Automation OFF starts it paused."""
    st = state(request)
    principal, form = await _owner(request)
    if _mode(form) == HFM_DEMO:
        return await _start_hfm(request, principal, _settings(form))
    chosen = _settings(form)
    balance = _dec(form, "starting_balance", chosen.amount, label="Starting balance")
    assert balance is not None
    now = st.clock.now()
    async with st.db.transaction() as conn:
        account = await sim.create(
            conn,
            name=str(form.get("name") or f"Gold simulation {now:%d %b %H:%M:%S}"),
            starting_balance=balance,
            settings=chosen,
            owner=principal.user_id,
            now=now,
        )
        if str(form.get("automation") or "ON") == "OFF":
            await sim.automation(conn, account, False, now)
    return RedirectResponse("/gold/sim?started=1", status_code=303)


async def _start_hfm(request: Request, principal: Any, chosen: sim.Settings) -> Response:
    """HFM Demo START (S65). First the size at HFM's rules with HFM's actual commission: when
    the owner's amount cannot carry HFM's smallest gold trade, nothing starts and the simulation
    is offered with the same amount. Otherwise, in one transaction: HFM's measured commission
    becomes the risk policy's fee for HFM (with this same authenticated press, the evidence in
    the reason), any earlier auto-trader on the HFM demo is replaced, and the gold reading's
    mandate starts."""
    st = state(request)
    terms = _hfm_terms(chosen)
    now = st.clock.now()
    async with st.db.transaction() as conn:
        reading, price = await board.start_view(
            conn, await sim.reading_now(conn, now), await sim.price_now(conn, now)
        )
        preview = await board.hfm_preview(conn, chosen, reading, price)
        if preview.why and preview.why.startswith(TOO_SMALL):
            return await render(
                request,
                "gold_sim_offer.html",
                principal,
                chosen=chosen,
                why=preview.why,
                status_code=200,
            )
        costs = preview.costs
        _, policy = await active_policy(conn)
        params, why = policy.model_dump(), []
        if (
            costs is not None
            and costs.measured
            and policy.fee_bps_for("HFM") != costs.bps
            and not any(f.venue == "HFM" for f in policy.venue_fees)
        ):
            params["venue_fees"] = (
                *params.get("venue_fees", ()),
                {"venue": "HFM", "bps": costs.bps},
            )
            why.append(
                f"HFM's own records: {costs.charged:,.2f} commission and fees on {costs.deals} "
                f"gold deals ({costs.bps:f} bps a side), instead of the general "
                f"{policy.fee_bps_assumption:f} bps assumption"
            )
        if chosen.allow_sell != ("HFM" in policy.short_venues):  # the owner's direction (S66)
            params["short_venues"] = tuple(v for v in policy.short_venues if v != "HFM") + (
                ("HFM",) if chosen.allow_sell else ()
            )
            why.append(
                "short selling of HFM's gold (a CFD, demo) allowed: the owner chose buy and sell"
                if chosen.allow_sell
                else "no short selling on HFM: the owner chose buy only"
            )
        if why:
            await change_policy(
                conn,
                principal=principal,
                new_params=params,
                reason=("; ".join(why) + "; applied with the owner's START on My Gold Auto-Trader")[
                    :500
                ],
                now=now,
                settings=st.settings,
            )
        account = await get_account(conn, alias=HFM_ACCOUNT)
        running = await conn.scalar(
            select(trading_mandates.c.id).where(
                trading_mandates.c.account_id == account.id,
                trading_mandates.c.status == "ACTIVE",
            )
        )
        granted = await mandates.grant(
            conn,
            principal=principal,
            terms=terms,
            now=now,
            settings=st.settings,
            replace=running is not None,  # never refused by an earlier one; never off and on
        )
        m = await mandates.get_mandate(conn, granted)
        if m.mode == "AUTO":  # the proof, at once, under the same authorisation (S67)
            await hfm_selftest.start(conn, m, principal.user_id, now)
    return RedirectResponse("/gold/sim?started=hfm", status_code=303)


@router.post("/gold/hfm/selftest", include_in_schema=False)
async def gold_hfm_selftest(request: Request) -> Response:
    """The HFM demo self-test again: one buy and one short of 1 ounce under the running mandate,
    each closed by itself, then reconciled. It places trades, so it needs the owner's code."""
    st = state(request)
    principal, _ = await _owner(request)
    now = st.clock.now()
    async with st.db.transaction() as conn:
        found = await hfm_demo.gold_mandates(conn, now)
        if not found:
            raise ConflictError("start HFM Demo first: the self-test runs under it")
        await hfm_selftest.start(conn, found[0], principal.user_id, now)
    return RedirectResponse("/gold/sim", status_code=303)


def _account(form: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(form.get("account") or ""))
    except ValueError:
        raise ValidationFailedError("which simulation?") from None


@router.post("/gold/sim/settings", include_in_schema=False)
async def gold_sim_settings(request: Request) -> Response:
    """New settings for the running simulation: the next trade is sized by them."""
    st = state(request)
    principal, form = await _owner(request)
    async with st.db.transaction() as conn:
        await sim.save_settings(
            conn, _account(form), _settings(form), principal.user_id, st.clock.now()
        )
    return RedirectResponse("/gold/sim", status_code=303)


@router.post("/gold/sim/automation", include_in_schema=False)
async def gold_sim_automation(request: Request) -> Response:
    """The Automation switch. OFF needs no code (it only stops new trades; the open ones keep
    their stop-loss and take-profit); ON needs the owner's code, as START does."""
    st = state(request)
    on = str((await request.form()).get("automation") or "") == "ON"
    if on:
        _, form = await _owner(request)
    else:
        _, form = await authorize_form(request, Capability.MANAGE_APPROVED_POSITION)
    async with st.db.transaction() as conn:
        await sim.automation(conn, _account(form), on, st.clock.now())
    return RedirectResponse("/gold/sim", status_code=303)


@router.post("/gold/hfm/off", include_in_schema=False)
async def gold_hfm_off(request: Request) -> Response:
    """HFM Demo automation OFF: its mandate ends, so no new trade is proposed. Open positions at
    HFM keep their stop-loss and take-profit. No code: it only takes risk away."""
    st = state(request)
    principal, _ = await authorize_form(request, Capability.MANAGE_APPROVED_POSITION)
    now = st.clock.now()
    async with st.db.transaction() as conn:
        for m in await hfm_demo.gold_mandates(conn, now):
            await mandates.end(
                conn,
                mandate_id=m.id,
                status="REVOKED",
                by=principal.actor,
                reason="automation switched off by the owner",
                now=now,
            )
    return RedirectResponse("/gold/sim", status_code=303)


def _daily_terms(alias: str, form: Any) -> mandates.MandateTerms:
    """The owner's amount and risk for the daily robot (S75): Automatic, one trade at a time, a
    day may lose one trade's worth, and it stops for good at ten trades' worth or the whole
    amount, whichever is less. Their numbers are never changed: what cannot hold is refused."""
    amount = _dec(form, "amount", None, label="Amount")
    risk = _dec(form, "risk_pct", None, label="Risk per trade")
    if amount is None or risk is None or amount <= 0:
        raise ValidationFailedError("Amount and risk per trade: give a positive number each")
    per_trade = (amount * risk / 100).quantize(Decimal("0.01"), ROUND_DOWN)
    try:
        terms = mandates.terms_from_money(
            account_alias=alias,
            money=amount,
            care="NORMAL",
            mode="AUTO",
            instruments=(hfm_demo.GOLD,),
            strategy_key=mandates.GOLD_DAILY,
            timeframe=mandates.DAILY,
            days=mandates.UNTIL_STOPPED_DAYS,
            risk_pct=risk,
            open_positions=1,
            daily_loss=per_trade,
        )
    except ConflictError as exc:
        raise ValidationFailedError(str(exc)) from None
    return terms.model_copy(
        update={"max_trades_per_day": 1, "total_loss_limit": min(terms.total_limit, amount)}
    )


HUNDRED = Decimal(100)  # the owner's real start (2026-11-10: "fix all thing to 100")


@router.post("/gold/hundred", include_in_schema=False)
async def gold_hundred(request: Request) -> Response:
    """The 100 USD robot, with the owner's code (S76, S78): its own simulated account with 100
    USD on HFM's live prices, from 0.01 ounce, under the owner's rules (2 USD a trade with its
    costs, a 4 USD target, 6 USD a day, a pause after 3 losses in a row). Isolated: it has its
    own account, state and limits, and it never touches the robots at HFM or their trades.
    Nothing is sent to HFM."""
    st = state(request)
    principal, _ = await _owner(request)
    now = st.clock.now()
    rules = sim.defaults(HUNDRED)
    async with st.db.transaction() as conn:
        account_id = await sim.create(
            conn,
            name=f"100 USD from {now:%d %b %H:%M}",
            starting_balance=HUNDRED,
            settings=rules,
            owner=principal.user_id,
            now=now,
        )
        await audit(
            conn,
            action="gold_sim.started_100_usd",
            actor=principal.actor,
            occurred_at=now,
            entity_type="gold_sim_account",
            entity_id=account_id,
            payload={
                "starting_balance": str(HUNDRED),
                "risk_pct": str(rules.risk_pct),
                "daily_loss": str(rules.daily_loss),
                "take_profit_r": str(rules.take_profit_r),
                "pause_after_losses": sim.PAUSE_AFTER_LOSSES,
                "max_positions": rules.max_positions,
            },
        )
    return RedirectResponse("/gold/sim?started=hundred", status_code=303)


@router.post("/gold/daily/start", include_in_schema=False)
async def gold_daily_start(request: Request) -> Response:
    """START the daily robot on the second HFM demo account, with the owner's code (S75). An
    earlier daily robot there is replaced; the hourly robot is never touched."""
    st = state(request)
    principal, form = await _owner(request)
    now = st.clock.now()
    async with st.db.transaction() as conn:
        alias = await conn.scalar(
            select(accounts.c.alias).where(
                accounts.c.broker_adapter == mandates.GOLD_DAILY_ADAPTER,
                accounts.c.kind == "SANDBOX",
                accounts.c.status == "ACTIVE",
            )
        )
        if alias is None:
            raise ConflictError("the second HFM demo account is not open yet")
        terms = _daily_terms(alias, form)
        account = await get_account(conn, alias=alias)
        running = await conn.scalar(
            select(trading_mandates.c.id).where(
                trading_mandates.c.account_id == account.id,
                trading_mandates.c.status == "ACTIVE",
            )
        )
        await mandates.grant(
            conn,
            principal=principal,
            terms=terms,
            now=now,
            settings=st.settings,
            replace=running is not None,
        )
    return RedirectResponse("/gold/sim?started=daily", status_code=303)


@router.post("/gold/daily/off", include_in_schema=False)
async def gold_daily_off(request: Request) -> Response:
    """The daily robot OFF: its mandate ends, so no new trade is proposed. An open position at
    HFM keeps its stop-loss. No code: it only takes risk away."""
    st = state(request)
    principal, _ = await authorize_form(request, Capability.MANAGE_APPROVED_POSITION)
    now = st.clock.now()
    async with st.db.transaction() as conn:
        for m in await daily.daily_mandates(conn, now):
            await mandates.end(
                conn,
                mandate_id=m.id,
                status="REVOKED",
                by=principal.actor,
                reason="the daily robot switched off by the owner",
                now=now,
            )
    return RedirectResponse("/gold/sim", status_code=303)


@router.post("/gold/sim/answer", include_in_schema=False)
async def gold_sim_answer(request: Request) -> Response:
    """The owner's answer to a trade the simulation asked about, in the portal. Approving opens
    a simulated trade and needs their code (once; it stays fresh for a while); rejecting needs
    none. Never through Telegram."""
    st = state(request)
    approve = str((await request.form()).get("answer") or "") == "APPROVE"
    if approve:
        principal, form = await _owner(request)
    else:
        principal, form = await authorize_form(request, Capability.MANAGE_APPROVED_POSITION)
    try:
        decision = uuid.UUID(str(form.get("decision") or ""))
    except ValueError:
        raise ValidationFailedError("which trade?") from None
    async with st.db.transaction() as conn:
        await sim.answer(conn, decision, approve, principal.user_id, st.clock.now())
    return RedirectResponse("/gold/sim", status_code=303)


@router.post("/gold/sim/stop", include_in_schema=False)
async def gold_sim_stop(request: Request) -> Response:
    """Stop it: every open simulated trade closes at HFM's price now (or the next one), and no
    new one opens. Stopping needs no code: it only takes risk away."""
    st = state(request)
    _, form = await authorize_form(request, Capability.MANAGE_APPROVED_POSITION)
    now = st.clock.now()
    async with st.db.transaction() as conn:
        account = _account(form)
        await sim.close_all(conn, account, now, "stopped by the owner")
        await sim.set_running(conn, account, False, "stopped by the owner", now)
    return RedirectResponse("/gold/sim", status_code=303)


@router.post("/gold/sim/resume", include_in_schema=False)
async def gold_sim_resume(request: Request) -> Response:
    st = state(request)
    _, form = await _owner(request)
    async with st.db.transaction() as conn:
        await sim.resume(conn, _account(form), st.clock.now())
    return RedirectResponse("/gold/sim", status_code=303)
