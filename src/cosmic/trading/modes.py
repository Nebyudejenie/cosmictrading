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
    if account_kind == "PAPER":
        if mode in PAPER_EXPOSURE_MODES:
            return True, "paper trading is permitted"
        return False, f"{mode.value} does not permit paper orders"
    if mode in LIVE_MODES:
        return True, "live trading mode with per-trade authorisation"
    return False, f"{mode.value} has no live order-submission authority"
