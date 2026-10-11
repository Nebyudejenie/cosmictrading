"""Role declarations (WF-02): every role states its mission, limits, tools, budget and reviewer."""

from __future__ import annotations

from dataclasses import dataclass

from cosmic.identity.capabilities import SERVICE_CAPABILITIES, Capability, ServiceIdentity

RESEARCH_CAPS = SERVICE_CAPABILITIES[ServiceIdentity.RESEARCH_WORKER]


@dataclass(frozen=True)
class Role:
    key: str
    mission: str
    out_of_scope: str
    inputs: str
    output_schema: str
    allowed_tools: frozenset[str]
    data_classification: str
    capabilities: frozenset[Capability]
    sla_seconds: int
    max_attempts: int
    ai_call_budget: int
    evidence_requirements: tuple[str, ...]
    quality_gates: tuple[str, ...]
    reviewer: str | None
    metrics: tuple[str, ...]
    failure_modes: tuple[str, ...]
    retirement: str
    uses_ai: bool


ROLES: dict[str, Role] = {
    r.key: r
    for r in (
        Role(
            "orchestrator",
            "Open missions for material events and sequence the tasks.",
            "Analysis, proposals, approvals, execution.",
            "market events",
            "mission + task graph",
            frozenset({"ops.read"}),
            "INTERNAL_NONSENSITIVE",
            frozenset(),
            60,
            3,
            0,
            ("triggering event id",),
            ("idempotent per event", "depth ≤ 3"),
            None,
            ("missions opened", "missions expired"),
            ("duplicate events", "stalled dependencies"),
            "Retire if missions are opened manually only.",
            False,
        ),
        Role(
            "source_data_analyst",
            "Verify data freshness, completeness and cross-venue agreement.",
            "Forecasting, proposals.",
            "instrument",
            "DataCheck",
            frozenset({"market.read", "evidence.claim"}),
            "PUBLIC",
            RESEARCH_CAPS & {Capability.READ_MARKET_DATA},
            120,
            3,
            0,
            ("quote timestamp", "bar count", "cross-venue deviation"),
            ("labels reported, never upgraded",),
            "task_reviewer",
            ("stale sources found",),
            ("source outage", "schema change"),
            "Retire when a licensed real-time feed with its own quality service exists.",
            False,
        ),
        Role(
            "quant_analyst",
            "Compute indicators, regime, strategy signal, backtest and sizing.",
            "Narrative claims, approvals, execution.",
            "instrument + data check",
            "QuantAnalysis",
            frozenset({"market.read", "research.analyse", "trading.size", "evidence.claim"}),
            "INTERNAL_NONSENSITIVE",
            RESEARCH_CAPS - {Capability.PROPOSE_TRADE},
            300,
            3,
            0,
            ("bar range", "backtest run id", "assumptions"),
            ("no look-ahead", "costs included", "disclaimer present"),
            "task_reviewer",
            ("signals", "backtest drawdown"),
            ("insufficient history",),
            "Retire a strategy version when walk-forward results degrade.",
            False,
        ),
        Role(
            "market_researcher",
            "Write thesis, counterarguments, scenarios and red-team view from evidence.",
            "Numbers not present in evidence, approvals, execution, tools.",
            "evidence packet references",
            "Narrative",
            frozenset({"ai.research_narrative", "evidence.claim"}),
            "PUBLIC",
            RESEARCH_CAPS - {Capability.PROPOSE_TRADE},
            600,
            3,
            2,
            ("evidence ids cited",),
            ("schema-valid JSON", "numeric claims re-verified by code"),
            "task_reviewer",
            ("claims verified vs inferred",),
            ("AI unavailable", "invalid output", "prompt injection in sources"),
            "Retire if AI output is consistently contradicted by verified data.",
            True,
        ),
        Role(
            "risk_reviewer",
            "Turn a qualified opportunity into a typed candidate and run deterministic pre-checks.",
            "Approving, submitting, changing limits.",
            "quant analysis + narrative",
            "ProposalResult",
            frozenset({"trading.propose", "trading.size", "market.read"}),
            "INTERNAL_NONSENSITIVE",
            RESEARCH_CAPS,
            120,
            2,
            0,
            ("risk check run id",),
            ("NON_EXECUTABLE when any check fails",),
            "task_reviewer",
            ("proposals", "non-executable rate"),
            ("stale data", "limit breach"),
            "Never retired: required for any proposal.",
            False,
        ),
        Role(
            "task_reviewer",
            "Verify each task output against its acceptance criteria before it counts as done.",
            "Producing analysis.",
            "task output",
            "TaskReview",
            frozenset({"ops.read"}),
            "INTERNAL_NONSENSITIVE",
            frozenset(),
            60,
            3,
            0,
            ("checks list",),
            ("completion requires verification",),
            None,
            ("tasks reopened",),
            ("over-lenient checks",),
            "Never retired.",
            False,
        ),
        Role(
            "ops_security_monitor",
            "Watch health, quotas, incidents and audit integrity.",
            "Trading decisions.",
            "system state",
            "OpsReport",
            frozenset({"ops.read"}),
            "INTERNAL_NONSENSITIVE",
            frozenset(),
            300,
            3,
            0,
            ("incident ids",),
            ("no secrets in reports",),
            None,
            ("incidents raised",),
            ("alert fatigue",),
            "Never retired.",
            False,
        ),
    )
}

# Hard boundary: no role may hold execution, approval or administration capabilities.
FORBIDDEN_FOR_ROLES = frozenset(
    {
        Capability.APPROVE_TRADE,
        Capability.SUBMIT_ORDER,
        Capability.CANCEL_ORDER,
        Capability.MANAGE_APPROVED_POSITION,
        Capability.CHANGE_RISK_LIMITS,
        Capability.ENABLE_STRATEGY_AUTHORITY,
        Capability.ADMINISTER_SYSTEM,
    }
)
FORBIDDEN_TOOLS = frozenset(
    {"trading.execute", "trading.approve", "broker.submit", "shell", "db.write"}
)
