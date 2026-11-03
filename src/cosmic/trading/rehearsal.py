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
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, CosmicError, NotFoundError, ValidationFailedError
from cosmic.core.logs import get_logger
from cosmic.db.schema_execution import trade_intents
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require
from cosmic.notify.outbox import notify_owners
from cosmic.trading import mandates, swap
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.authority import get_state
from cosmic.trading.brokers.paper import PaperBroker
from cosmic.trading.context_builder import account_state
from cosmic.trading.modes import Operation, account_fields, mode_permits
from cosmic.trading.paper_accounts import open_paper_account
from cosmic.trading.policy import active_policy
from cosmic.trading.risk import rooms


@dataclass(frozen=True)
class Venue:
    key: str  # in forms and links
    alias: str
    market: str
    venue: str
    currency: str
    name: str
    fee_hint: str  # what to set under Risk policy → venue_fees
    fee_bps: Decimal = Decimal(0)  # the published fee a side, offered when none is set (S46)
    fee_basis: str = ""  # where that fee comes from, in plain words


log = get_logger(__name__)

REHEARSALS: dict[str, Venue] = {
    "binance": Venue(
        "binance",
        mandates.REHEARSAL_PREFIX + "binance-100",
        "SPOT:PAXG/USDT@BINANCE",
        "BINANCE",
        "USDT",
        "Binance gold (PAXG)",
        "BINANCE=10 (Binance's regular 0.1 %; 7.5 with BNB)",
        Decimal(10),
    ),
    "hfm-cent": Venue(
        "hfm-cent",
        mandates.REHEARSAL_PREFIX + "hfm-cent-100",
        "CFD:XAU/USD@HFM_CENT",
        "HFM_CENT",
        "USD",
        "HFM Cent terms, on HFM's gold prices",
        "HFM_CENT=0 (a Cent account charges no commission)",
        fee_basis="a Cent account charges no commission",
    ),
}
FIRST = REHEARSALS["binance"]
ALIAS, MARKET, VENUE, CURRENCY = FIRST.alias, FIRST.market, FIRST.venue, FIRST.currency
START = Decimal(100)
CENT = Decimal("0.01")
CARE = "BOLD"  # the owner's choice, 2026-10-24: 2 % of the money a trade
FLOOR = Decimal(80)  # the owner's choice: it stops for good after losing $20 of the $100
DAYS = 90
NEEDS_YOU = "it needs you: "  # a renewal blocker the owner must act on


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
    auto_renew: bool = False,
) -> tuple[uuid.UUID, bool]:
    """Open the venue's rehearsal account (first time) and grant its daily auto-trader. Returns
    (the account, whether it was opened now). Opening needs ADMINISTER_SYSTEM; the grant needs
    APPROVE_TRADE with a fresh code (``mandates.grant``). ``auto_renew``: the owner's choice that
    each run renews itself when it ends (S49, ``renew_due``)."""
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
        money, opened, reserve = START, True, Decimal(0)
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
        try:
            money, reserve = await renewal_money(conn, acct, now)
        except Wait as exc:
            raise ConflictError(str(exc)) from None
    terms = run_terms(
        v, money, rule, auto_renew, reserve, await _kept(conn, v, money - reserve, now)
    )
    await mandates.grant(
        conn, principal=principal, terms=terms, now=now, settings=settings, rehearsal=True
    )
    return account_id, opened


class Wait(Exception):
    """A renewal that cannot happen yet but will by itself shortly (an entry on its way, no
    fresh price for the open trade)."""


async def renewal_money(
    conn: AsyncConnection, acct: Account, now: datetime
) -> tuple[Decimal, Decimal]:
    """What a new run starts from: the account's money rounded down to the cent, and the open
    trade's worst case at its stop (R19's open risk) with the overnight swap it has run up and
    could pay over a run (S54), kept apart so the floor still holds when the new run takes the
    trade over. ``Wait`` while an entry is on its way or the open trade has no fresh price."""
    state = await account_state(conn, acct, now)
    if state.equity is None:
        raise Wait("its money is not known yet (no fresh price)")
    if await _entry_in_flight(conn, acct.id):
        raise Wait("an entry is on its way; it renews once that has filled or ended")
    reserve = rooms.open_risk(state)
    if reserve is None:
        raise Wait("its open trade has no fresh price yet")
    reserve += await swap.accrued(conn, acct.id, now)
    for pos in state.positions or ():
        if pos.mark_price is not None:
            reserve += await swap.ahead(
                conn, (pos.instrument_key,), pos.quantity * pos.mark_price, now
            )
    money = state.equity.quantize(CENT, rounding=ROUND_DOWN)
    return money, reserve.quantize(CENT, rounding=ROUND_UP)


def run_terms(
    v: Venue,
    money: Decimal,
    rule: str,
    auto_renew: bool,
    reserve: Decimal = Decimal(0),
    kept: Decimal = Decimal(0),
) -> mandates.MandateTerms:
    """A run of the rehearsal from ``money``: Bold, daily, automatic, 90 days, stopping for good
    at the floor of $80, also after a renewal.

    ``reserve``: an open trade's worst case, taken over by the new run. It is kept out of the
    run's money, so the floor holds even if that trade is stopped out and stays exactly 80 for
    the database's renewal guard (a floor raised by it would refuse the next renewal; audit).
    ``kept``: the swap its stop for good keeps aside for one more trade (S54): a new run needs
    room for a full loss and that."""
    base = money - reserve
    terms = mandates.terms_from_money(
        account_alias=v.alias,
        money=base,
        care=CARE,
        mode="AUTO",
        instruments=(v.market,),
        strategy_key=rule,
        timeframe=mandates.DAILY,
        days=DAYS,
    )
    room = base - FLOOR
    if room < terms.max_loss_per_trade + kept:
        raise ConflictError(
            f"the rehearsal holds {money:,.2f}: its floor of {FLOOR:,.2f}"
            + (f", less its open trade's worst case of {reserve:,.2f}," if reserve else "")
            + (f" with the overnight swap kept aside ({kept:,.2f})" if kept else "")
            + " leaves no room for another trade"
        )
    return terms.model_copy(update={"total_loss_limit": room, "auto_renew": auto_renew})


async def _expired_to_renew(conn: AsyncConnection, acct: Account) -> mandates.Mandate | None:
    """The account's last run, if it ended at its expiry with renewal chosen and not stopped."""
    last = await mandates.recent_mandates(conn, limit=1, account_id=acct.id)
    if not last:
        return None
    prev = last[0]
    if prev.status != "EXPIRED" or not prev.auto_renew:
        return None
    if await mandates.renewal_stopped(conn, prev.id):
        return None
    return prev


async def _renewal(
    conn: AsyncConnection, key: str, prev: mandates.Mandate, now: datetime, *, lock: bool = True
) -> mandates.MandateTerms:
    """The new run's terms, or why not: ``Wait`` (shortly, by itself) or ``ConflictError``
    (it needs the owner). ``lock``: hold the authority row, so a mode change under way decides
    first (the renewal); the page only reads."""
    authority = await get_state(conn, lock="share") if lock else await get_state(conn)
    ok, why = mode_permits(authority.mode, Operation.OPEN_EXPOSURE, account_kind="PAPER")
    if not ok:
        raise Wait(f"new trades are paused (mode {authority.mode.value}: {why})")
    v = REHEARSALS[key]
    acct = await account(conn, key)
    if acct is None or acct.status != "ACTIVE":
        raise ConflictError("its account is not active")
    if not await fee_set(conn, key):
        raise ConflictError(f"the {v.venue} fee is no longer set under Risk policy")
    if not await allowed(conn, key):
        raise ConflictError(f"your risk policy no longer allows {v.market}")
    money, reserve = await renewal_money(conn, acct, now)
    return run_terms(
        v, money, prev.strategy_key, True, reserve, await _kept(conn, v, money - reserve, now)
    )


async def _kept(conn: AsyncConnection, v: Venue, money: Decimal, now: datetime) -> Decimal:
    """The swap a new run's stop for good keeps aside for one more trade (S54)."""
    _, policy = await active_policy(conn)
    nxt = await mandates.next_trade_swap(conn, money, (v.market,), now, policy)
    return nxt.quantize(CENT, rounding=ROUND_UP)


async def renew_due(conn: AsyncConnection, now: datetime) -> list[uuid.UUID]:
    """Renew each rehearsal whose last run ended at its expiry with renewal chosen (S49), from
    the money it holds then, on the same rule, never with a lower floor; a trade it still holds
    is taken over by the new run (its worst case kept apart), so the rule goes on managing its
    exit. Never after a run that stopped itself or was revoked, or whose renewal the owner
    stopped; not while new paper trades are paused (the emergency stop among them). Each
    rehearsal in its own savepoint: one never undoes the other. A renewal that needs the owner
    is told once. Returns the new runs."""
    renewed: list[uuid.UUID] = []
    for key in REHEARSALS:
        acct = await account(conn, key)
        prev = None if acct is None else await _expired_to_renew(conn, acct)
        if prev is None:
            continue
        try:
            async with conn.begin_nested():
                terms = await _renewal(conn, key, prev, now)
                renewed.append(await mandates.renew(conn, prev, terms, now))
        except Wait:
            continue  # it renews by itself shortly
        except Exception as exc:  # it needs the owner: told once for this run
            log.warning("rehearsal_not_renewed", venue=key, error=type(exc).__name__)
            why = exc.message if isinstance(exc, CosmicError) else "an internal error (logged)"
            await _not_renewed(conn, prev, why, now)
    return renewed


async def renewal_blocker(conn: AsyncConnection, key: str, now: datetime) -> str | None:
    """Why the rehearsal's ended run has not renewed itself yet, in plain words (the page);
    None when it renews within minutes or does not renew itself."""
    acct = await account(conn, key)
    prev = None if acct is None else await _expired_to_renew(conn, acct)
    if prev is None:
        return None
    try:
        await _renewal(conn, key, prev, now, lock=False)
    except Wait as exc:
        return str(exc)
    except CosmicError as exc:
        return f"{NEEDS_YOU}{exc.message}"
    return None


async def _not_renewed(
    conn: AsyncConnection, prev: mandates.Mandate, why: str, now: datetime
) -> None:
    acct = await get_account(conn, account_id=prev.account_id)
    fields = account_fields(acct.alias, acct.kind, acct.broker_adapter)
    await notify_owners(
        conn,
        kind="mandate_changed",
        now=now,
        dedup_key=f"mandate:{prev.id}:not_renewed",
        payload={
            **fields,
            "text": (
                f"Your $100 rehearsal on {fields['account_ref']} could not renew itself: {why}. "
                "Start it again on Start with $100 when you are ready."
            ),
        },
    )


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
