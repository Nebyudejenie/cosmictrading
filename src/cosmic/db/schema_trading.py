"""Core tables: authority, accounts, risk, ledger, positions, go-live (migration 0003+)."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from cosmic.db.schema import NUM, TS, metadata

authority_state = Table(
    "authority_state",
    metadata,
    Column("id", SmallInteger, primary_key=True, autoincrement=False),
    Column("mode", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("mode_before_stop", Text),
    Column("changed_at", TS, nullable=False),
    Column("changed_by", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("live_activation_id", Uuid),
)

authority_transitions = Table(
    "authority_transitions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("from_mode", Text, nullable=False),
    Column("to_mode", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text),
    Column("reason", Text, nullable=False),
    Column("preconditions", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("occurred_at", TS, nullable=False),
)

accounts = Table(
    "accounts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("alias", Text, nullable=False, unique=True),
    Column("kind", Text, nullable=False),
    Column("venue_id", Uuid, ForeignKey("venues.id"), nullable=False),
    Column("broker_adapter", Text, nullable=False),
    Column("base_currency", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("reconciliation_status", Text, nullable=False),
    Column("last_reconciled_at", TS),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
)

risk_policies = Table(
    "risk_policies",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("version", Integer, nullable=False, unique=True),
    Column("params", JSONB, nullable=False),
    Column("params_hash", Text, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Index("risk_policies_one_active", "active", unique=True, postgresql_where=text("active")),
)

risk_check_runs = Table(
    "risk_check_runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("accounts.id")),
    Column("intent_id", Uuid),
    Column("order_id", Uuid),
    Column("stage", Text, nullable=False),
    Column("policy_version", Integer, nullable=False),
    Column("decision", Text, nullable=False),
    Column("exposure_increasing", Boolean, nullable=False),
    Column("results", JSONB, nullable=False),
    Column("context", JSONB, nullable=False),
    Column("created_at", TS, nullable=False),
    Index("risk_check_runs_intent", "intent_id"),
)

ledger_txns = Table(
    "ledger_txns",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("ref_type", Text),
    Column("ref_id", Text),
    Column("description", Text, nullable=False),
    Column("is_simulated", Boolean, nullable=False),
    Column("created_at", TS, nullable=False),
    UniqueConstraint("kind", "ref_type", "ref_id"),
)

ledger_entries = Table(
    "ledger_entries",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("txn_id", Uuid, ForeignKey("ledger_txns.id"), nullable=False),
    Column("account_id", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("book", Text, nullable=False),
    Column("asset", Text, nullable=False),
    Column("amount", NUM, nullable=False),
    Column("created_at", TS, nullable=False),
    Index("ledger_entries_account", "account_id", "book", "asset"),
    Index("ledger_entries_txn", "txn_id"),
)

positions = Table(
    "positions",
    metadata,
    Column("account_id", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("instrument_id", Uuid, ForeignKey("instruments.id"), nullable=False),
    Column("quantity", NUM, nullable=False),
    Column("avg_cost", NUM),
    Column("realized_pnl", NUM, nullable=False, server_default=text("0")),
    Column("protection_state", Text, nullable=False, server_default=text("'NONE'::text")),
    Column("updated_at", TS, nullable=False),
    PrimaryKeyConstraint("account_id", "instrument_id"),
)

equity_snapshots = Table(
    "equity_snapshots",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("taken_at", TS, nullable=False),
    Column("equity", NUM, nullable=False),
    Column("cash", NUM, nullable=False),
    Column("positions_value", NUM, nullable=False),
    Column("price_label", Text, nullable=False),
    Column("is_simulated", Boolean, nullable=False),
    Index("equity_snapshots_account", "account_id", "taken_at"),
)

golive_gates = Table(
    "golive_gates",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("gate_key", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("evidence", JSONB, nullable=False),
    Column("recorded_by", Text, nullable=False),
    Column("recorded_at", TS, nullable=False),
)

live_activations = Table(
    "live_activations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("activated_by", Uuid, ForeignKey("users.id"), nullable=False),
    Column("readiness_snapshot", JSONB, nullable=False),
    Column("activated_at", TS, nullable=False),
    Column("revoked_at", TS),
)
