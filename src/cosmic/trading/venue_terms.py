"""What an order means at each venue, in the words of the approval packet. Pure, no I/O.

The packet is what the owner approves, so it states the venue's real behaviour (ADR-028): a
broker stop-loss on an HFM position fills at the market price, CFDs trade in the broker's hours
(gold closes at weekends) and open CFD positions pay or earn an overnight swap.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ProtectionTerms:
    stop_type: str
    bracket: str
    stop_fill: str  # the loss estimate's assumption about the stop's fill


STOP_LIMIT = ProtectionTerms(
    "STOP_LIMIT sell, reduce-only, sized to filled quantity",
    "not available; protective orders are separate orders",
    "the stop-limit fills at its limit",
)
PROTECTION: dict[str, ProtectionTerms] = {
    "hfm_mt5_demo": ProtectionTerms(
        "broker stop-loss on the position (HFM): it triggers at the stop price and fills at the "
        "market price, which can be worse than the stop in a gap",
        "attached to the entry: the stop-loss is sent with the buy, and HFM holds it on its "
        "server from the fill, even when the bridge or the PC is offline",
        "the stop fills at the plan's stop-limit price (a slippage allowance; a market fill can "
        "be worse in a gap)",
    ),
}


def protection_terms(adapter: str) -> ProtectionTerms:
    return PROTECTION.get(adapter, STOP_LIMIT)


def session_text(asset_class: str) -> str:
    if asset_class == "CFD":
        return (
            "the broker's trading hours for this symbol (not 24x7: gold closes at weekends and "
            "for a daily break); while the market is closed quotes go stale and HFM refuses "
            "orders (10018 Market closed)"
        )
    return "24x7 (UTC)"


# MT5's ENUM_SYMBOL_SWAP_MODE and ENUM_DAY_OF_WEEK (Sunday is 0), as the bridge reports them.
SWAP_MODES = {
    0: "no swap",
    1: "points per lot",
    2: "the base currency per lot",
    3: "the margin currency per lot",
    4: "the account currency per lot",
    5: "a yearly rate on the current price",
    6: "a yearly rate on the opening price",
    7: "reopening at the close",
    8: "reopening at the bid",
}
MT5_DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
METALS = frozenset({"XAU", "XAG"})  # closed at weekends: their weekend swap is charged once, ×3


@dataclass(frozen=True)
class Swap:
    """The broker's overnight swap per night, as a share of the price (negative: paid)."""

    long: float
    short: float
    triple_day: str | None  # the weekday three nights are charged at once
    mode: str

    @property
    def triple_weekday(self) -> int | None:
        """The triple day as a Python weekday (Monday 0)."""
        return (MT5_DAYS.index(self.triple_day) + 6) % 7 if self.triple_day else None


def swap_per_night(metadata: Mapping[str, Any] | None, price: float) -> Swap | None:
    """The swap the bridge reported, converted to a share of ``price`` per night and per unit
    held. None when it is not known (an older bridge) or not money (a reopening mode).
    Money modes assume the account and margin currency is the price's currency (USD)."""
    m = metadata or {}
    try:
        mode = int(m["swap_mode"])
        long_, short_ = float(m["swap_long"]), float(m["swap_short"])
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0:
        return None
    contract = float(m.get("contract_size") or 0)
    point = float(m.get("point") or 0)
    if mode == 0:
        per = (0.0, 0.0)
    elif mode == 1 and point > 0:  # points per lot: per unit it is points × point, in price
        per = (long_ * point / price, short_ * point / price)
    elif mode in (3, 4) and contract > 0:  # money per lot per night
        per = (long_ / contract / price, short_ / contract / price)
    elif mode in (5, 6):  # a yearly percentage
        per = (long_ / 100 / 360, short_ / 100 / 360)
    else:
        return None
    day = m.get("swap_rollover3days")
    triple = MT5_DAYS[int(day)] if day is not None and 0 <= int(day) <= 6 else None
    return Swap(per[0], per[1], triple, SWAP_MODES.get(mode, str(mode)))


def funding_text(asset_class: str, metadata: Mapping[str, Any] | None) -> str:
    if asset_class != "CFD":
        return "none (spot, no leverage)"
    m = metadata or {}
    unit = SWAP_MODES.get(int(m["swap_mode"]), "") if m.get("swap_mode") is not None else ""
    rates = (
        f" At the last report: long {m['swap_long']}, short {m['swap_short']} per lot per "
        f"night, in {unit or 'the broker’s swap units'}."
        if m.get("swap_long") is not None and m.get("swap_short") is not None
        else ""
    )
    return (
        "overnight swap: the broker charges (or pays) it on open CFD positions each night; it "
        "is booked as a fee when charged and is not in the fee or loss estimates." + rates
    )


NEW_YORK = ZoneInfo("America/New_York")
ROLLOVER_NY_HOUR = 17  # the broker's day ends at 17:00 in New York: the swap is charged then


def nights(opened: datetime, closed: datetime, triple: int | None) -> int:
    """Swap nights between two times: each rollover (17:00 in New York) held over counts once.
    With ``triple`` (a market closed at weekends), Saturday and Sunday count nothing and that
    weekday three. Shared by the replay and the paper venue (S38)."""
    ny_open = (opened if opened.tzinfo else opened.replace(tzinfo=UTC)).astimezone(NEW_YORK)
    ny_close = (closed if closed.tzinfo else closed.replace(tzinfo=UTC)).astimezone(NEW_YORK)
    count = 0
    day, last = ny_open.date(), ny_close.date()
    while day <= last:
        rollover = datetime.combine(day, time(ROLLOVER_NY_HOUR), NEW_YORK)
        if opened < rollover <= closed:
            if triple is None:
                count += 1
            elif day.weekday() < 5:
                count += 3 if day.weekday() == triple else 1
        day += timedelta(days=1)
    return count
