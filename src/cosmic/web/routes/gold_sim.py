"""HFM gold only (S61-S63): the analysis of HFM's candles, and My Gold Auto-Trader, the one
panel where the owner chooses an amount, a mode, automation and permission (the system
calculates the rest), confirms START, answers the trades it asks about, and switches it off."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.core.errors import ValidationFailedError
from cosmic.gold import analysis, board, sim, timing
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import require
from cosmic.web.deps import authorize, authorize_form, state
from cosmic.web.views import render

router = APIRouter()
DEFAULT = sim.defaults(Decimal(50))
SIMULATION, HFM_DEMO = "SIMULATION", "HFM_DEMO"
HFM_DEMO_NOT_YET = (
    "HFM Demo mode: automatic orders on your HFM demo account from this analysis are not "
    "built yet. Choose Simulation: it trades on HFM's real prices with your amount, and "
    "nothing is sent to HFM. HFM's own demo is tested with one press on the HFM demo page."
)


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
    daily = _dec(form, "daily_loss", base.daily_loss, label="Most a day may lose")
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
    if mode == HFM_DEMO:
        raise ValidationFailedError(HFM_DEMO_NOT_YET)
    if mode != SIMULATION:
        raise ValidationFailedError("Trading mode: choose Simulation or HFM Demo")
    return mode


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
    _mode(form)
    chosen = _settings(form)
    wrong = sim.problems(chosen, chosen.amount)
    if wrong:
        raise ValidationFailedError(" ".join(wrong))
    on = str(form.get("automation") or "ON") != "OFF"
    async with st.db.connect() as conn:
        b = await board.build(conn, st.clock.now(), amount=chosen.amount)
    b.calc = board.calculate(chosen, sim.fresh_state(chosen.amount), b.reading, b.price)
    return await render(
        request, "gold_sim_confirm.html", principal, b=b, chosen=chosen, automation_on=on
    )


@router.post("/gold/sim/start", include_in_schema=False)
async def gold_sim_start(request: Request) -> Response:
    """START, confirmed with the owner's code: a new simulation whose starting balance is their
    amount (or the balance they gave), with their settings. The one before stops, its records
    kept. Automation OFF starts it paused."""
    st = state(request)
    principal, form = await _owner(request)
    _mode(form)
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
