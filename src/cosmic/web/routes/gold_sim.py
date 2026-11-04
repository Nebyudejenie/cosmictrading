"""HFM gold only (S61, S62): the analysis of HFM's candles, and the internal simulation."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.core.errors import ValidationFailedError
from cosmic.gold import analysis, board, sim
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import require
from cosmic.web.deps import authorize, authorize_form, state
from cosmic.web.views import render

router = APIRouter()
DEFAULT = sim.Settings(amount=Decimal(50), risk_pct=Decimal(2), daily_loss=Decimal(3))


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
    tp = str(form.get("take_profit_r") or "").strip().lower()
    take_profit = (
        None if tp in ("", "none") else _dec(form, "take_profit_r", None, label="Take-profit")
    )
    try:
        positions = int(str(form.get("max_positions") or DEFAULT.max_positions))
    except ValueError:
        raise ValidationFailedError("Trades open at once: write a whole number") from None
    amount = _dec(form, "amount", None, label="Trading amount")
    risk = _dec(form, "risk_pct", None, label="Risk per trade")
    daily = _dec(form, "daily_loss", None, label="Most a day may lose")
    if amount is None or risk is None or daily is None:
        raise ValidationFailedError(
            "Trading amount, risk per trade and the most a day may lose are needed"
        )
    open_risk = _dec(form, "max_open_risk_pct", DEFAULT.max_open_risk_pct, label="At risk at once")
    drop = _dec(form, "max_drawdown_pct", DEFAULT.max_drawdown_pct, label="Largest drop")
    assert open_risk is not None and drop is not None
    return sim.Settings(
        amount=amount,
        risk_pct=risk,
        daily_loss=daily,
        max_open_risk_pct=open_risk,
        max_drawdown_pct=drop,
        max_positions=positions,
        take_profit_r=take_profit,
        allow_sell=str(form.get("allow_sell") or "") == "on",
    )


@router.get("/gold/analysis", include_in_schema=False)
async def gold_analysis(request: Request) -> Response:
    """HFM gold on M1, M5, M15, H1 and D1 now: trend, momentum, volatility, support and
    resistance, and BUY, SELL or WAIT with the reasons."""
    principal = await authorize(request, Capability.READ_MARKET_DATA)
    st = state(request)
    now = st.clock.now()
    async with st.db.connect() as conn:
        reading = await sim.reading_now(conn, now)
        price = await sim.price_now(conn, now)
    return await render(
        request,
        "gold_analysis.html",
        principal,
        reading=reading,
        price=price,
        names=analysis.NAMES,
        enough=analysis.ENOUGH,
    )


@router.get("/gold/sim", include_in_schema=False)
async def gold_sim(request: Request) -> Response:
    """The internal gold simulation (Mode A): SIMULATED, never sent to HFM."""
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    async with st.db.connect() as conn:
        b = await board.build(conn, st.clock.now())
    return await render(request, "gold_sim.html", principal, b=b, default=DEFAULT)


async def _owner(request: Request) -> tuple[Any, Any]:
    st = state(request)
    principal, form = await authorize_form(request, Capability.APPROVE_TRADE)
    require(principal, Capability.APPROVE_TRADE, now=st.clock.now(), settings=st.settings)
    return principal, form


@router.post("/gold/sim/start", include_in_schema=False)
async def gold_sim_start(request: Request) -> Response:
    """A new simulation with the owner's starting balance and settings, with their code."""
    st = state(request)
    principal, form = await _owner(request)
    balance = _dec(form, "starting_balance", None, label="Starting balance")
    if balance is None:
        raise ValidationFailedError("Starting balance: it is needed")
    async with st.db.transaction() as conn:
        await sim.create(
            conn,
            name=str(form.get("name") or "Gold simulation"),
            starting_balance=balance,
            settings=_settings(form),
            owner=principal.user_id,
            now=st.clock.now(),
        )
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


@router.post("/gold/sim/stop", include_in_schema=False)
async def gold_sim_stop(request: Request) -> Response:
    """Stop it: every open simulated trade closes at HFM's price now, and no new one opens."""
    st = state(request)
    _, form = await _owner(request)
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
