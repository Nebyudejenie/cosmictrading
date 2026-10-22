"""Plain-text Telegram messages. No secrets, keys or account numbers — aliases only."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, environment_of

STATUS_LABEL = {
    "PROPOSED": "PROPOSED",
    "APPROVED": "APPROVED",
    "SUBMITTED": "SUBMITTED",
    "ACKNOWLEDGED": "ACKNOWLEDGED",
    "PARTIALLY_FILLED": "PARTIALLY FILLED",
    "FILLED": "FILLED",
    "REJECTED": "REJECTED",
    "CANCELLED": "CANCELLED",
    "UNKNOWN_ORDER_STATE": "UNKNOWN",
    "UNKNOWN": "UNKNOWN",
}


def _cents(value: Any) -> str:
    """Money estimates to the cent; packets store numbers as strings."""
    try:
        return f"{Decimal(str(value)).quantize(Decimal('0.01'), ROUND_HALF_EVEN):,}"
    except InvalidOperation:
        return str(value)


def _when(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def environment_tag(account: Mapping[str, Any]) -> str:
    """Every proposal names its environment (PAPER, TESTNET, DEMO or LIVE); simulated ones say
    so first. Packets from before the adapter was recorded fall back to the account kind."""
    env = environment_of(account["kind"], account.get("adapter"))
    sim = "SIMULATED · " if account["kind"] in SIMULATED_ACCOUNT_KINDS else ""
    return f" [{sim}{env.label}]"


def _card_terms(packet: dict[str, Any]) -> list[str]:
    """The approved terms in a few lines (directive Phase 3). Older packets lack some parts;
    what is missing is left out, never invented."""
    lines: list[str] = []
    inst, o = packet.get("instrument") or {}, packet.get("order") or {}
    ccy = (packet.get("costs") or {}).get("currency") or inst.get("quote") or ""
    if inst.get("canonical_key"):
        lines.append(f"Instrument: {inst['canonical_key']} (venue {inst.get('venue', '?')})")
    if o.get("limit_price") is not None:
        band = o.get("price_band") or {}
        slip = o.get("max_slippage_bps")
        lines.append(
            f"Order: {o.get('order_type')} {o['limit_price']} {ccy}, {o.get('time_in_force')} · "
            f"band {band.get('low')}–{band.get('high')} · slippage ≤ {slip} bps"
        )
    m = packet.get("market_at_proposal") or {}
    if m.get("bid") is not None:
        when = _when(m["quote_ts"]) if isinstance(m.get("quote_ts"), str) else m.get("quote_ts")
        lines.append(f"Quote: bid {m['bid']} / ask {m['ask']} at {when} ({m.get('label')})")
    plan = packet.get("management_plan") or {}
    if plan:
        stop, tp = plan.get("stop_loss"), plan.get("take_profit")
        lines.append(
            f"Stop-loss: {stop['stop_price'] if stop else 'NONE'} · "
            f"take-profit: {tp['price'] if tp else 'none'}"
        )
    r, c = packet.get("risk") or {}, packet.get("costs") or {}
    money: list[str] = []
    if o.get("max_notional") is not None:
        money.append(f"Notional ≤ {_cents(o['max_notional'])} {ccy}")
    if r:
        loss, pct = r.get("estimated_max_loss"), r.get("estimated_max_loss_pct_of_equity")
        money.append(
            "est. loss at stop "
            + (f"{_cents(loss)} {ccy}" if loss is not None else "NOT BOUNDED by an order")
            + (f" ({_cents(pct)}% of equity)" if pct is not None else "")
        )
    if c.get("estimated_fee") is not None:
        money.append(f"est. fee {_cents(c['estimated_fee'])} {ccy} (assumed)")
    if money:
        lines.append(" · ".join(money))
    s = packet.get("strategy")
    if s:
        version = f" v{s['version']}" if s.get("version") is not None else ""
        lines.append(f"Strategy: {s.get('key') or s.get('name')}{version}")
    checks = packet.get("checks")
    if checks:
        failed = [ch["code"] for ch in checks if ch["status"] not in ("PASS", "PASSED")]
        lines.append(
            f"Risk checks: {len(checks) - len(failed)}/{len(checks)} passed"
            + (f" — not passed: {', '.join(failed)}" if failed else "")
        )
    return lines


def proposal_summary(trade_code: str, packet: dict[str, Any], status: str) -> str:
    pl = packet["plain_language"]
    tag = environment_tag(packet["account"])
    ask = str(pl.get("why", "")).startswith("TIME TO TRADE") and status == "PROPOSED"
    return "\n".join(
        [
            *(["⏰ TIME TO TRADE — your auto-trader asks first"] if ask else []),
            f"{trade_code} — {STATUS_LABEL.get(status, status)}{tag}",
            f"Action: {pl['action']}",
            f"Account: {packet['account']['alias']}",
            *_card_terms(packet),
            f"Why: {pl['why']}",
            f"Key risk: {pl['key_risk']}",
            f"Expires: {_when(pl['expires'])}",
        ]
    )


def section(packet: dict[str, Any], name: str) -> str:
    """One part of the proposal, sent as its own message, so it names the account and its
    environment again (DIR-02)."""
    head = f"{packet['account']['alias']}{environment_tag(packet['account'])}"
    return f"{head}\n{_section(packet, name)}"


def _section(packet: dict[str, Any], name: str) -> str:
    if name == "details":
        o, c = packet["order"], packet["costs"]
        band = o["price_band"]
        return "\n".join(
            [
                f"Order: {o['side']} {o['quantity']} {packet['instrument']['canonical_key']}",
                f"{o['order_type']} limit {o['limit_price']} (band {band['low']}–{band['high']}, "
                f"slippage ≤ {o['max_slippage_bps']} bps, {o['time_in_force']})",
                f"Est. fee {_cents(c['estimated_fee'])} {c['currency']} ({c['fee_basis']})",
                f"Horizon: {packet['horizon']}",
                f"Thesis: {packet['thesis']}",
            ]
        )
    if name == "evidence":
        lines = [
            f"- {e['kind']}: {e['summary']} ({e.get('source_ts') or 'no timestamp'})"
            for e in packet["evidence"]
        ]
        return "Evidence:\n" + "\n".join(lines)
    if name == "counter":
        lines = [f"- {c}" for c in packet["counterarguments"]]
        return "Counterarguments:\n" + "\n".join(lines) + f"\nRed team: {packet['red_team']}"
    if name == "risk":
        r = packet["risk"]
        checks = "\n".join(f"{c['code']} {c['status']}: {c['message']}" for c in packet["checks"])
        max_loss = (
            f"{_cents(r['estimated_max_loss'])} {packet['costs']['currency']}"
            if r["estimated_max_loss"]
            else "not bounded by an order"
        )
        return f"Estimated max loss: {max_loss}\n{r['warning']}\nChecks:\n{checks}"
    if name == "impact":
        r = packet["risk"]
        return (
            f"Position after trade: {r.get('position_after')}\n"
            f"Concentration after trade: {r.get('concentration_pct_after')}%"
        )
    return "Unknown section."


def terms_account(terms: dict[str, Any]) -> str:
    """The account in confirmation terms with its environment, SIMULATED first when it is."""
    sim = "SIMULATED · " if terms.get("account_kind") in SIMULATED_ACCOUNT_KINDS else ""
    return f"{terms['account']} ({sim}{terms['environment']})"


def confirmation(terms: dict[str, Any]) -> str:
    plan = terms["management_plan"]
    stop = plan["stop_loss"]
    tp = plan["take_profit"]
    stop_text = f"stop {stop['stop_price']} / limit {stop['limit_price']}" if stop else "none"
    band = terms["price_band"]
    return "\n".join(
        [
            f"CONFIRM EXACT TRADE {terms['trade_code']}",
            f"Account: {terms_account(terms)}",
            f"Instrument: {terms['instrument']}",
            f"Side/quantity: {terms['side']} {terms['quantity']}",
            f"Order: {terms['order_type']} limit {terms['limit_price']} {terms['time_in_force']}",
            f"Price band: {band['low']}–{band['high']}",
            f"Stop-loss: {stop_text}",
            f"Take-profit: {tp['price'] if tp else 'none'}",
            *(
                [f"Closes {', '.join(terms['closes'])}: their protective stops are cancelled first"]
                if terms.get("closes")
                else []
            ),
            "Approval does not guarantee the order is accepted or filled.",
        ]
    )
