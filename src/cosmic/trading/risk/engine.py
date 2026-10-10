"""Evaluate a risk context (ADR-013).

Rule: every check must PASS. UNKNOWN blocks exposure-increasing operations
(unknown risk is never zero risk) and also blocks reduce-only operations, whose
reduced check set only contains facts that must be known to avoid over-selling.
"""

from __future__ import annotations

from dataclasses import dataclass

from cosmic.trading.policy import RiskPolicyParams
from cosmic.trading.risk.checks import EXPOSURE_CHECKS, REDUCE_CHECKS, CheckResult, Status
from cosmic.trading.risk.context import RiskContext


@dataclass(frozen=True)
class RiskDecision:
    passed: bool
    exposure_increasing: bool
    results: tuple[CheckResult, ...]

    @property
    def blocking(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is not Status.PASS)

    def plain_summary(self) -> str:
        if self.passed:
            return "All mandatory risk checks passed."
        return "Blocked: " + " | ".join(f"{r.code} {r.message}" for r in self.blocking)


def evaluate(ctx: RiskContext, policy: RiskPolicyParams) -> RiskDecision:
    increasing = ctx.request.exposure_increasing
    checks = EXPOSURE_CHECKS if increasing else REDUCE_CHECKS
    results: list[CheckResult] = []
    for check in checks:
        try:
            results.append(check(ctx, policy))
        except Exception as exc:  # a broken check must never pass silently
            results.append(
                CheckResult(
                    check.__name__[:3].upper(),
                    check.__name__,
                    Status.UNKNOWN,
                    f"check could not be evaluated ({type(exc).__name__})",
                )
            )
    passed = all(r.status is Status.PASS for r in results)
    return RiskDecision(passed=passed, exposure_increasing=increasing, results=tuple(results))
