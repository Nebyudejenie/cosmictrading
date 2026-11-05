"""The internal gold simulation (migration 0020, S62, ADR-037)."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from cosmic.db.schema import NUM, TS, metadata

gold_sim_accounts = Table(
    "gold_sim_accounts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("starting_balance", NUM, nullable=False),
    Column("currency", Text, nullable=False),
    Column("created_by", Uuid, ForeignKey("users.id"), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("status", Text, nullable=False),
    Column("status_changed_at", TS, nullable=False),
    Column("status_reason", Text, nullable=False),
)

# Append-only: each change of the owner's settings is a new row; the latest counts.
gold_sim_settings = Table(
    "gold_sim_settings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("gold_sim_accounts.id"), nullable=False),
    Column("amount", NUM, nullable=False),
    Column("risk_pct", NUM, nullable=False),
    Column("daily_loss", NUM, nullable=False),
    Column("max_open_risk_pct", NUM, nullable=False),
    Column("max_drawdown_pct", NUM, nullable=False),
    Column("max_positions", Integer, nullable=False),
    Column("take_profit_r", NUM),
    Column("allow_sell", Boolean, nullable=False),
    Column("permission", Text, nullable=False),
    Column("created_by", Uuid, ForeignKey("users.id"), nullable=False),
    Column("created_at", TS, nullable=False),
    Index("gold_sim_settings_latest", "account_id", "created_at"),
)

# Append-only: one decision per 1-minute candle (the key stops a restart deciding twice).
gold_sim_decisions = Table(
    "gold_sim_decisions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("gold_sim_accounts.id"), nullable=False),
    Column("bar", TS, nullable=False),
    Column("at", TS, nullable=False),
    Column("decision", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("reasons", JSONB, nullable=False),
    Column("bid", NUM),
    Column("ask", NUM),
    Column("ounces", NUM),
    Column("stop_distance", NUM),
    Column("position_id", Uuid),
    UniqueConstraint("account_id", "bar"),
)

# My Gold Auto-Trader on the HFM demo (S64, migration 0021): each decision of the gold reading
# under its mandate, once per 1-minute candle; append-only.
gold_hfm_decisions = Table(
    "gold_hfm_decisions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("mandate_id", Uuid, ForeignKey("trading_mandates.id"), nullable=False),
    Column("bar", TS, nullable=False),
    Column("at", TS, nullable=False),
    Column("decision", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("reasons", JSONB, nullable=False),
    Column("bid", NUM),
    Column("ask", NUM),
    Column("ounces", NUM),
    Column("stop_distance", NUM),
    Column("intent_id", Uuid, ForeignKey("trade_intents.id")),
    UniqueConstraint("mandate_id", "bar"),
    Index("gold_hfm_decisions_at", "mandate_id", "at"),
)

# The HFM demo self-test (S67, migration 0023): one row per run, advancing with it.
gold_hfm_selftests = Table(
    "gold_hfm_selftests",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("mandate_id", Uuid, ForeignKey("trading_mandates.id"), nullable=False),
    Column("requested_by", Uuid, ForeignKey("users.id"), nullable=False),
    Column("requested_at", TS, nullable=False),
    Column("phase", Text, nullable=False),
    Column("buy_intent", Uuid, ForeignKey("trade_intents.id")),
    Column("sell_intent", Uuid, ForeignKey("trade_intents.id")),
    Column("outcome", Text),
    Column("evidence", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("updated_at", TS, nullable=False),
    Column("finished_at", TS),
    Index(
        "gold_hfm_selftests_open",
        "mandate_id",
        postgresql_where=text("finished_at IS NULL"),
    ),
)

# The AI's second opinion on HFM's gold (S68, migration 0024): append-only; the gold engine reads
# the latest (it may only stop a trade).
ai_gold_opinions = Table(
    "ai_gold_opinions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("at", TS, nullable=False),
    Column("bar", TS),
    Column("provider_key", Text, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("reading", Text, nullable=False),
    Column("direction", Text, nullable=False),
    Column("recommendation", Text, nullable=False),
    Column("confidence", Text, nullable=False),
    Column("why", Text, nullable=False),
    Column("conditions", Text, nullable=False),
    Column("expected_risk", Text, nullable=False),
    Column("timing", Text, nullable=False),
    Index("ai_gold_opinions_at", "at"),
)

# A position changes once, from OPEN to CLOSED.
gold_sim_positions = Table(
    "gold_sim_positions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("gold_sim_accounts.id"), nullable=False),
    Column("decision_id", Uuid, ForeignKey("gold_sim_decisions.id"), nullable=False, unique=True),
    Column("side", Text, nullable=False),
    Column("ounces", NUM, nullable=False),
    Column("entry_price", NUM, nullable=False),
    Column("entry_at", TS, nullable=False),
    Column("stop_price", NUM, nullable=False),
    Column("take_profit_price", NUM),
    Column("spread", NUM, nullable=False),
    Column("margin", NUM, nullable=False),
    Column("status", Text, nullable=False),
    Column("exit_price", NUM),
    Column("exit_at", TS),
    Column("exit_reason", Text),
    Column("result", NUM),
    Index("gold_sim_positions_open", "account_id", postgresql_where=text("status = 'OPEN'::text")),
)

# Append-only: every change of the balance, once (``ref``); the balance is its sum.
gold_sim_ledger = Table(
    "gold_sim_ledger",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("gold_sim_accounts.id"), nullable=False),
    Column("at", TS, nullable=False),
    Column("kind", Text, nullable=False),
    Column("amount", NUM, nullable=False),
    Column("position_id", Uuid, ForeignKey("gold_sim_positions.id")),
    Column("ref", Text, nullable=False, unique=True),
    Column("note", Text, nullable=False),
    Index("gold_sim_ledger_account", "account_id", "at"),
)

# Append-only: the owner's answer to a trade the simulation asked about ("Ask me first"), once.
gold_sim_answers = Table(
    "gold_sim_answers",
    metadata,
    Column("decision_id", Uuid, ForeignKey("gold_sim_decisions.id"), primary_key=True),
    Column("answer", Text, nullable=False),
    Column("answered_by", Uuid, ForeignKey("users.id"), nullable=False),
    Column("answered_at", TS, nullable=False),
)
