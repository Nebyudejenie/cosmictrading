"""Core tables: workforce and research (migration 0006)."""

from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from cosmic.db.schema import TS, metadata

strategies = Table(
    "strategies",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("key", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("created_at", TS, nullable=False),
)
strategy_versions = Table(
    "strategy_versions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("strategy_id", Uuid, ForeignKey("strategies.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("params", JSONB, nullable=False),
    Column("code_ref", Text, nullable=False),
    Column("hash", Text, nullable=False, unique=True),
    Column("status", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    UniqueConstraint("strategy_id", "version"),
)
backtest_runs = Table(
    "backtest_runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("strategy_version_id", Uuid, ForeignKey("strategy_versions.id"), nullable=False),
    Column("instrument_id", Uuid, ForeignKey("instruments.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("timeframe", Text, nullable=False),
    Column("data_from", TS, nullable=False),
    Column("data_to", TS, nullable=False),
    Column("bars", Integer, nullable=False),
    Column("data_vintage", JSONB, nullable=False),
    Column("assumptions", JSONB, nullable=False),
    Column("trials", Integer, nullable=False),
    Column("result", JSONB, nullable=False),
    Column("result_hash", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Index("backtest_runs_version", "strategy_version_id", "created_at"),
)
missions = Table(
    "missions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("trigger_event_id", Uuid, ForeignKey("market_events.id")),
    Column("instrument_id", Uuid, ForeignKey("instruments.id")),
    Column("status", Text, nullable=False),
    Column("status_reason", Text),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("ai_calls_used", Integer, nullable=False, server_default=text("0")),
    Column("ai_call_budget", Integer, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("completed_at", TS),
    Index("missions_status", "status"),
)
opportunities = Table(
    "opportunities",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("mission_id", Uuid, ForeignKey("missions.id"), nullable=False),
    Column("instrument_id", Uuid, ForeignKey("instruments.id"), nullable=False),
    Column("strategy_version_id", Uuid, ForeignKey("strategy_versions.id")),
    Column("status", Text, nullable=False),
    Column("direction", Text),
    Column("thesis", Text),
    Column("intent_id", Uuid, ForeignKey("trade_intents.id")),
    Column("summary", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
)
opportunity_transitions = Table(
    "opportunity_transitions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("opportunity_id", Uuid, ForeignKey("opportunities.id"), nullable=False),
    Column("from_status", Text),
    Column("to_status", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("occurred_at", TS, nullable=False),
)
tasks = Table(
    "tasks",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("mission_id", Uuid, ForeignKey("missions.id"), nullable=False),
    Column("parent_task_id", Uuid, ForeignKey("tasks.id")),
    Column("role_key", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default=text("100")),
    Column("depth", Integer, nullable=False, server_default=text("0")),
    Column("deadline", TS),
    Column("expires_at", TS, nullable=False),
    Column("input", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("output", JSONB),
    Column("acceptance", JSONB, nullable=False),
    Column("blocker", Text),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("3")),
    Column("ai_calls_used", Integer, nullable=False, server_default=text("0")),
    Column("ai_call_budget", Integer, nullable=False, server_default=text("0")),
    Column("reviewer_role", Text),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("completed_at", TS),
    Index("tasks_runnable", "status", "priority"),
)
task_deps = Table(
    "task_deps",
    metadata,
    Column("task_id", Uuid, ForeignKey("tasks.id"), nullable=False),
    Column("depends_on", Uuid, ForeignKey("tasks.id"), nullable=False),
    PrimaryKeyConstraint("task_id", "depends_on"),
)
task_attempts = Table(
    "task_attempts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("task_id", Uuid, ForeignKey("tasks.id"), nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("worker_id", Text, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("finished_at", TS),
    Column("status", Text, nullable=False),
    Column("error", Text),
)
task_reviews = Table(
    "task_reviews",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("task_id", Uuid, ForeignKey("tasks.id"), nullable=False),
    Column("reviewer_role", Text, nullable=False),
    Column("verdict", Text, nullable=False),
    Column("checks", JSONB, nullable=False),
    Column("created_at", TS, nullable=False),
)
artifacts = Table(
    "artifacts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("mission_id", Uuid, ForeignKey("missions.id"), nullable=False),
    Column("task_id", Uuid, ForeignKey("tasks.id")),
    Column("kind", Text, nullable=False),
    Column("content", JSONB, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Index("artifacts_mission", "mission_id"),
)
escalations = Table(
    "escalations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("task_id", Uuid, ForeignKey("tasks.id"), nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("resolved_at", TS),
)


# ---- strategy library (migration 0008) — all append-only --------------------------------------

library_runs = Table(
    "library_runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("started_at", TS, nullable=False),
    Column("finished_at", TS, nullable=False),
    Column("code_version", Text, nullable=False),
    Column("trials", Integer, nullable=False),
    Column("costs", JSONB, nullable=False),
    Column("series", JSONB, nullable=False),
    Column("summary", JSONB, nullable=False),
    Column("result_hash", Text, nullable=False),
    Column("created_at", TS, nullable=False),
)

# ---- the rule competition (migration 0014, ADR-031) — append-only ----------------------------

auto_competitions = Table(
    "auto_competitions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("created_at", TS, nullable=False),
    Column("code_version", Text, nullable=False),
    Column("forward_from", TS, nullable=False),
    Column("data_until", TS, nullable=False),
    Column("trials", Integer, nullable=False),
    Column("result", JSONB, nullable=False),
    Column("result_hash", Text, nullable=False),
    Index("auto_competitions_latest", "created_at"),
)

strategy_evaluations = Table(
    "strategy_evaluations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("library_run_id", Uuid, ForeignKey("library_runs.id"), nullable=False),
    Column("strategy_version_id", Uuid, ForeignKey("strategy_versions.id"), nullable=False),
    Column("series", Text, nullable=False),
    Column("instrument_id", Uuid, ForeignKey("instruments.id")),
    Column("tier", Text, nullable=False),
    Column("rank", Integer, nullable=False),
    Column("deflated_sharpe", Numeric(18, 8), nullable=False),
    Column("timing_sharpe", Numeric(18, 8), nullable=False),
    Column("result", JSONB, nullable=False),
    Column("created_at", TS, nullable=False),
    UniqueConstraint("library_run_id", "strategy_version_id", "series"),
    Index("strategy_evaluations_run", "library_run_id", "series", "rank"),
)

strategy_activations = Table(
    "strategy_activations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("strategy_key", Text, nullable=False),
    Column("instrument_id", Uuid, ForeignKey("instruments.id"), nullable=False),
    Column("action", Text, nullable=False),
    Column("evaluation_id", Uuid, ForeignKey("strategy_evaluations.id")),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("occurred_at", TS, nullable=False),
    Index("strategy_activations_latest", "strategy_key", "instrument_id", "occurred_at"),
)
