"""The MT5 brokers the system knows by name (S115).

HFM runs the robots' demo accounts and is the market data source. The owner's real account for
Manual Trading may also be at Exness (the owner's choice of 2026-11-23); it is only ever read,
through terminal 3 with its investor (read-only) password. A broker is recognised from MT5's own
``account_info.company`` (never from what the owner typed).

``published_clock``: whether the broker's server clock follows a published rule the system
checks the bridge's measure against. HFM's is New York + 7 hours (``hfm_candles``). No Exness
rule is checked here: what the bridge measures from a price that has just arrived is used, and
only once the bridge says so (bridge 13, ``server_utc_offset_confirmed``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mt5Broker:
    name: str  # as the pages say it
    company: str  # MT5's account_info.company starts with it
    published_clock: bool


HFM = Mt5Broker("HFM", "HF Markets", True)
EXNESS = Mt5Broker("Exness", "Exness", False)
KNOWN = (HFM, EXNESS)


def broker_of(company: str | None) -> Mt5Broker | None:
    """The known broker of an MT5 account from its reported company, or None."""
    name = (company or "").strip()
    return next((b for b in KNOWN if name.startswith(b.company)), None)


def names() -> str:
    """The known brokers in words, for example "HFM or Exness"."""
    return " or ".join(b.name for b in KNOWN)
