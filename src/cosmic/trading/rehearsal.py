"""The $100 rehearsals (the $100 Gold Plan, S37–S38): simulated money treated exactly like the
owner's real $100 to come, on two venues to compare.

One action per venue, with the owner's code, opens a PAPER account holding 100 of the venue's
currency, used by nothing else, and grants it the daily auto-trader on that venue's gold with the
owner's choices: Bold (2 % of the money a trade) and a stop for good once $20 of the $100 is
lost. Every limit, check, stop, report and review applies as to any auto-trader.

- **Binance gold (PAXG):** paper fills on Binance's prices (S37).
- **HFM Cent terms:** paper fills on HFM's own gold prices from the bridge, with the Cent
  account's published lots (0.01 oz), no commission, and HFM's swap charged at the exit (S38).
  It stands in for a real Cent demo until one can be read through a bridge.

- **Renewing** (the mandate ends after 90 days) starts again from the account's money now: the
  money grows or shrinks in steps, and the stop for good stays at $80, the $100 less the $20.
- **A rehearsal that stopped itself** at that floor is not restarted: that is its result.
- **The fee** comes first. Paper fills charge the risk policy's fee for the venue, and without a
  value the 0.6 % assumption would be charged; the action refuses until the owner sets it, and
  until the risk policy allows the venue's market.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, NotFoundError, ValidationFailedError
from cosmic.db.schema_execution import trade_intents
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require
from cosmic.trading import mandates
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.brokers.paper import PaperBroker
from cosmic.trading.context_builder import account_state
from cosmic.trading.paper_accounts import open_paper_account
from cosmic.trading.policy import active_policy


@dataclass(frozen=True)
class Venue:
    key: str  # in forms and links
    alias: str
    market: str
    venue: str
    currency: str
    name: str
    fee_hint: str  # what to set under Risk policy → venue_fees


REHEARSALS: dict[str, Venue] = {
    "binance": Venue(
        "binance",
        mandates.REHEARSAL_PREFIX + "binance-100",
        "SPOT:PAXG/USDT@BINANCE",
        "BINANCE",
        "USDT",
        "Binance gold (PAXG)",
        "BINANCE=10 (Binance's regular 0.1 %; 7.5 with BNB)",
    ),
    "hfm-cent": Venue(
        "hfm-cent",
        mandates.REHEARSAL_PREFIX + "hfm-cent-100",
        "CFD:XAU/USD@HFM_CENT",
        "HFM_CENT",
        "USD",
        "HFM Cent terms, on HFM's gold prices",
        "HFM_CENT=0 (a Cent account charges no commission)",
    ),
}
FIRST = REHEARSALS["binance"]
ALIAS, MARKET, VENUE, CURRENCY = FIRST.alias, FIRST.market, FIRST.venue, FIRST.currency
START = Decimal(100)
CARE = "BOLD"  # the owner's choice, 2026-10-24: 2 % of the money a trade
FLOOR = Decimal(80)  # the owner's choice: it stops for good after losing $20 of the $100
DAYS = 90


async def account(conn: AsyncConnection, venue: str = "binance") -> Account | None:
    try:
        return await get_account(conn, alias=REHEARSALS[venue].alias)
    except NotFoundError:
        return None


async def fee_set(conn: AsyncConnection, venue: str = "binance") -> bool:
    """The owner has set the venue's fee in the risk policy (B-08)."""
    _, policy = await active_policy(conn)
    return any(f.venue == REHEARSALS[venue].venue for f in policy.venue_fees)


async def allowed(conn: AsyncConnection, venue: str = "binance") -> bool:
    """The risk policy allows the venue's market (R04), or every proposal would be refused."""
    _, policy = await active_policy(conn)
    v = REHEARSALS[venue]
    return (
        v.market in policy.allowed_instruments
        and v.venue in policy.allowed_venues
        and v.currency in policy.allowed_quote_currencies
    )


async def start(
    conn: AsyncConnection,
    *,
    principal: Principal,
    broker: PaperBroker,
    rule: str,
    now: datetime,
    settings: Settings,
    venue: str = "binance",
) -> tuple[uuid.UUID, bool]:
    """Open the venue's rehearsal account (first time) and grant its daily auto-trader. Returns
    (the account, whether it was opened now). Opening needs ADMINISTER_SYSTEM; the grant needs
    APPROVE_TRADE with a fresh code (``mandates.grant``)."""
    require(principal, Capability.ADMINISTER_SYSTEM, now=now, settings=settings)
    if venue not in REHEARSALS:
        raise ValidationFailedError("choose Binance or HFM Cent")
    v = REHEARSALS[venue]
    if rule not in mandates.RULES_BY_TIMEFRAME[mandates.DAILY]:
        raise ValidationFailedError("choose one of the daily rules")
    if not await fee_set(conn, venue):
        raise ConflictError(
            f"set the {v.venue} fee first: Risk policy → venue_fees, for example {v.fee_hint}; "
            "until then paper fills would charge the 0.6 % assumption"
        )
    if not await allowed(conn, venue):
        raise ConflictError(
            f"your risk policy does not allow {v.market} yet: add it, {v.venue} and {v.currency} "
            "to its lists under Risk policy"
        )
    acct = await account(conn, venue)
    if acct is None:
        account_id = await open_paper_account(
            conn,
            broker=broker,
            alias=v.alias,
            initial_capital=START,
            now=now,
            actor=principal.actor,
            venue_key=v.venue,
            base_currency=v.currency,
        )
        money, opened = START, True
    else:
        account_id, opened = acct.id, False
        if any(m.account_id == acct.id for m in await mandates.active_mandates(conn, now)):
            raise ConflictError("the $100 rehearsal is already running")
        last = await mandates.recent_mandates(conn, limit=1, account_id=acct.id)
        if last and last[0].status == "STOPPED":
            raise ConflictError(
                "the rehearsal stopped itself for good at its floor (80.00 of the 100): that is "
                "its result, and it is not restarted"
            )
        state = await account_state(conn, acct, now)
        if state.equity is None or state.positions is None:
            raise ConflictError("the rehearsal account's money is unknown; reconcile it first")
        if any(p.quantity > 0 for p in state.positions) or await _entry_in_flight(conn, acct.id):
            # the open trade's result would land outside the new mandate's limits
            raise ConflictError(
                "the rehearsal still holds a trade or one is on its way; renew it once that "
                "trade has closed (its stop stays in place meanwhile)"
            )
        money = state.equity.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    terms = mandates.terms_from_money(
        account_alias=v.alias,
        money=money,
        care=CARE,
        mode="AUTO",
        instruments=(v.market,),
        strategy_key=rule,
        timeframe=mandates.DAILY,
        days=DAYS,
    )
    room = money - FLOOR  # the stop for good stays at $80, also after a renewal
    if room < terms.max_loss_per_trade:
        raise ConflictError(
            f"the rehearsal holds {money:,.2f}: its floor of {FLOOR:,.2f} leaves no room for "
            "another trade"
        )
    terms = terms.model_copy(update={"total_loss_limit": room})
    await mandates.grant(
        conn, principal=principal, terms=terms, now=now, settings=settings, rehearsal=True
    )
    return account_id, opened


async def _entry_in_flight(conn: AsyncConnection, account_id: uuid.UUID) -> bool:
    found = await conn.scalar(
        select(trade_intents.c.id)
        .where(
            trade_intents.c.account_id == account_id,
            trade_intents.c.side == "BUY",
            trade_intents.c.status.in_(mandates.IN_FLIGHT),
        )
        .limit(1)
    )
    return found is not None
