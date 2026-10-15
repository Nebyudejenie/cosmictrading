"""What an order means at each venue, in the words of the approval packet. Pure, no I/O.

The packet is what the owner approves, so it states the venue's real behaviour (ADR-028): a
broker stop-loss on an HFM position fills at the market price, CFDs trade in the broker's hours
(gold closes at weekends) and open CFD positions pay or earn an overnight swap.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


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
            "the broker's trading hours for this symbol (not always 24x7: gold closes at "
            "weekends); while the market is closed quotes go stale and orders are refused"
        )
    return "24x7 (UTC)"


def funding_text(asset_class: str, metadata: Mapping[str, Any] | None) -> str:
    if asset_class != "CFD":
        return "none (spot, no leverage)"
    m = metadata or {}
    rates = (
        f" At the last report: long {m['swap_long']}, short {m['swap_short']} per lot per "
        "night, in the broker's swap units."
        if m.get("swap_long") is not None and m.get("swap_short") is not None
        else ""
    )
    return (
        "overnight swap: the broker charges (or pays) it on open CFD positions each night; it "
        "is booked as a fee when charged and is not in the fee or loss estimates." + rates
    )
