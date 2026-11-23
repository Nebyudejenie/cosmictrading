"""The recommendation for the setup now (S117): TAKE, YOUR CALL or SKIP, from the rule's tested
record against what the trade needs to break even after the spread.

The probability is the rule's own record in markets like this one (side and volatility): how
often such trades reached the target first, with its 95 % range. It stays HISTORICAL SIMULATION
· IN SAMPLE, unverified, until the forward test has enough trades. It is never called a
prediction, and it is kept apart from the setup checks (how many tested conditions agree).

- TAKE: even the low end of the 95 % range is above break-even.
- YOUR CALL: the record is above break-even, but its range reaches below it (no proven edge).
- SKIP: not above break-even, a reason to stand aside, or no setup.
- UNTESTED: the plan's reward differs from the one the record was measured at (1:2).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from cosmic.gold import trend_buy
from cosmic.manual.insight import NO_TRADE, Insight

TAKE, YOUR_CALL, SKIP, UNTESTED = "TAKE", "YOUR CALL", "SKIP", "UNTESTED"


@dataclass(frozen=True)
class Advice:
    level: str
    why: str
    side: str | None = None
    p: Decimal | None = None  # the target reached first, in the record
    low: Decimal | None = None  # its 95 % range
    high: Decimal | None = None
    n: int = 0
    verified: bool = False
    breakeven: Decimal | None = None  # the share of winners it needs, after the spread
    cost_r: Decimal | None = None  # the spread, in units of the risk

    @property
    def tag(self) -> str:
        return {TAKE: "tag-ok", YOUR_CALL: "tag-warn"}.get(self.level, "tag-bad")


def _pct(x: Decimal) -> str:
    return f"{x * 100:.1f} %"


def advise(idea: Insight, rr: Decimal, spread: Decimal | None) -> Advice:
    """The recommendation for Insight's setup at the plan's reward ``rr``."""
    pr = idea.probability
    if idea.rule_side is None or idea.entry is None or idea.stop is None or pr is None:
        return Advice(SKIP, f"no setup now: {idea.why}")
    distance = abs(idea.entry - idea.stop)
    cost = (spread or Decimal(0)) / distance if distance else Decimal(0)
    breakeven = (1 + cost) / (rr + 1)
    base = Advice(
        SKIP,
        "",
        side=idea.rule_side,
        p=pr.p,
        low=pr.low,
        high=pr.high,
        n=pr.n,
        verified=pr.verified,
        breakeven=breakeven,
        cost_r=cost,
    )
    if rr != trend_buy.REWARD:
        return replace(
            base,
            level=UNTESTED,
            why=f"the rule's record is for a 1:{trend_buy.REWARD.normalize():f} target; at "
            f"1:{rr.normalize():f} how often the target comes first is not measured",
        )
    if idea.direction == NO_TRADE:
        return replace(base, level=SKIP, why="; ".join(idea.no_trade) or idea.why)
    needs = f"the {_pct(breakeven)} it needs to break even after the spread"
    record = (
        f"{_pct(pr.p)} of {pr.n} such trades reached the target first "
        f"(95 % range {_pct(pr.low)}–{_pct(pr.high)})"
    )
    if pr.low > breakeven:
        return replace(base, level=TAKE, why=f"{record}: even its low end is above {needs}")
    if pr.p > breakeven:
        return replace(
            base,
            level=YOUR_CALL,
            why=f"{record}: above {needs}, but its range reaches below it, so no proven edge",
        )
    return replace(base, level=SKIP, why=f"{record}: not above {needs}")
