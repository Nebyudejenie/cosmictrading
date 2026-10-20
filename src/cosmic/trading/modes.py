"""Authority modes (V6 §1804) and what each permits. Pure, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
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
# test network or demo account, ADR-026/028). Both are authorised like paper, labelled SIMULATED.
SIMULATED_ACCOUNT_KINDS = frozenset({"PAPER", "SANDBOX"})


@dataclass(frozen=True)
class Environment:
    """Where an account's orders go, named the same way on every page, message and record."""

    code: str  # PAPER | TESTNET | DEMO | LIVE
    where: str
    money: str

    @property
    def label(self) -> str:
        return f"{self.code} — {self.where}, {self.money}"


# Keyed by (account kind, broker adapter) as stored on the account.
ENVIRONMENTS: dict[tuple[str, str], Environment] = {
    ("PAPER", "paper"): Environment("PAPER", "paper simulator", "simulated money"),
    ("SANDBOX", "binance_spot_testnet"): Environment(
        "TESTNET", "Binance Spot Test Network", "virtual funds"
    ),
    ("SANDBOX", "hfm_mt5_demo"): Environment(
        "DEMO", "HFM MetaTrader 5 demo account", "virtual funds"
    ),
}


def environment_of(kind: str, adapter: str | None = None) -> Environment:
    """An unknown pair never reads as safer than it is: anything that is not PAPER or SANDBOX
    is LIVE."""
    env = ENVIRONMENTS.get((kind, adapter or ""))
    if env is not None:
        return env
    if kind == "PAPER":
        return Environment("PAPER", "paper simulator", "simulated money")
    if kind == "SANDBOX":
        return Environment(
            "TESTNET", f"test venue ({adapter or 'adapter unknown'})", "virtual funds"
        )
    return Environment("LIVE", f"live venue ({adapter or 'adapter unknown'})", "real money")


def environment_short(kind: str, adapter: str | None = None) -> str:
    """The environment in a few words for alerts and lists (DIR-02): ``SIMULATED · DEMO`` for an
    account without real money, ``LIVE · real money`` for anything else."""
    env = environment_of(kind, adapter)
    if kind in SIMULATED_ACCOUNT_KINDS:
        return f"SIMULATED · {env.code}"
    return f"{env.code} · {env.money}"


def account_ref(alias: str, kind: str, adapter: str | None = None) -> str:
    """How an account is named in every alert, notification and list: ``hfm-demo [SIMULATED ·
    DEMO]``. The alias alone is free text and never says whether the money is real."""
    return f"{alias} [{environment_short(kind, adapter)}]"


def account_fields(alias: str, kind: str, adapter: str | None = None) -> dict[str, object]:
    """The account and its environment as record fields (audit payloads, logs, notifications)."""
    return {
        "account": alias,
        "environment": environment_of(kind, adapter).code,
        "simulated": kind in SIMULATED_ACCOUNT_KINDS,
        "account_ref": account_ref(alias, kind, adapter),
    }


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
