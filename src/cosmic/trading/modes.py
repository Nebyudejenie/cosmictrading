"""Authority modes (V6 §1804) and what each permits. Pure, no I/O."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    PAPER_TRADING = "PAPER_TRADING"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    STRATEGY_AUTHORIZED = "STRATEGY_AUTHORIZED"
    PROTECTION_ONLY = "PROTECTION_ONLY"
    EMERGENCY_STOP = "EMERGENCY_STOP"


# Ordering by permissiveness. APPROVED_EXECUTION is a per-order authority, not a mode (ADR-010).
PERMISSIVENESS: dict[Mode, int] = {
    Mode.EMERGENCY_STOP: 0,
    Mode.PROTECTION_ONLY: 1,
    Mode.RESEARCH_ONLY: 2,
    Mode.PAPER_TRADING: 3,
    Mode.APPROVAL_REQUIRED: 4,
    Mode.STRATEGY_AUTHORIZED: 5,
}
LIVE_MODES = frozenset({Mode.APPROVAL_REQUIRED, Mode.STRATEGY_AUTHORIZED})
PAPER_EXPOSURE_MODES = frozenset(
    {Mode.PAPER_TRADING, Mode.APPROVAL_REQUIRED, Mode.STRATEGY_AUTHORIZED}
)
# Account kinds without real money: our paper simulator, and SANDBOX (virtual funds on a venue's
# test network, ADR-026). Both are authorised like paper and labelled SIMULATED.
SIMULATED_ACCOUNT_KINDS = frozenset({"PAPER", "SANDBOX"})


class Operation(StrEnum):
    OPEN_EXPOSURE = "OPEN_EXPOSURE"  # new position or increase
    REDUCE_EXPOSURE = "REDUCE_EXPOSURE"  # reduce/close; approved exits and protection
    CANCEL = "CANCEL"


def is_more_permissive(target: Mode, current: Mode) -> bool:
    return PERMISSIVENESS[target] > PERMISSIVENESS[current]


def mode_permits(mode: Mode, operation: Operation, *, account_kind: str) -> tuple[bool, str]:
    """Whether ``mode`` permits ``operation`` on an account of ``account_kind``.

    Returns (allowed, plain-language reason).
    """
    if operation is Operation.CANCEL:
        return True, "cancelling an order never adds exposure"
    if operation is Operation.REDUCE_EXPOSURE:
        if mode is Mode.EMERGENCY_STOP:
            return (
                True,
                "only explicitly authorised protective operations are allowed in an emergency stop",
            )
        return True, "reducing exposure is allowed in every mode"
    # OPEN_EXPOSURE
    if mode in (Mode.EMERGENCY_STOP, Mode.PROTECTION_ONLY):
        return False, f"{mode.value}: new exposure is blocked"
    if mode is Mode.RESEARCH_ONLY:
        return False, "RESEARCH_ONLY: no orders may be placed, not even simulated ones"
    if account_kind in SIMULATED_ACCOUNT_KINDS:
        what = "paper" if account_kind == "PAPER" else "test-network (virtual funds)"
        if mode in PAPER_EXPOSURE_MODES:
            return True, f"{what} trading is permitted"
        return False, f"{mode.value} does not permit {what} orders"
    if mode in LIVE_MODES:
        return True, "live trading mode with per-trade authorisation"
    return False, f"{mode.value} has no live order-submission authority"


# What each mode means, in the words the portal shows next to the choice.
DESCRIPTIONS: dict[Mode, str] = {
    Mode.RESEARCH_ONLY: "Research and proposals only. No orders, not even simulated ones.",
    Mode.PAPER_TRADING: "Simulated trades on a paper account; each one still needs your approval.",
    Mode.APPROVAL_REQUIRED: "Live trading, each trade approved by you.",
    Mode.STRATEGY_AUTHORIZED: "Live trading within a strategy you authorised.",
    Mode.PROTECTION_ONLY: "No new positions. Only exits and protective orders.",
    Mode.EMERGENCY_STOP: "Everything new is blocked until the recovery checklist is met.",
}


def selectable_targets(current: Mode, *, cap: Mode, live_available: bool) -> list[Mode]:
    """Modes an owner can switch to from ``current`` with an ordinary mode change.

    Emergency stop has its own button; modes above the deployment cap and live modes whose
    preconditions are unmet would only be refused, so they are not offered.
    """
    return [
        m
        for m in Mode
        if m not in (current, Mode.EMERGENCY_STOP)
        and PERMISSIVENESS[m] <= PERMISSIVENESS[cap]
        and (live_available or m not in LIVE_MODES)
    ]
