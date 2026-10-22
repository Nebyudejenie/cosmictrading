"""Core tables: intents, approvals, orders, fills, reconciliation, paper venue (migration 0004)."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from cosmic.db.schema import NUM, TS, metadata


def _id() -> Column[object]:
    return Column("id", Uuid, primary_key=True)


trade_intents = Table(
    "trade_intents",
    metadata,
    _id(),
    Column("trade_code", Text, nullable=False, unique=True),
    Column("account_id", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("instrument_id", Uuid, ForeignKey("instruments.id"), nullable=False),
    Column(
        "opportunity_id",
        Uuid,
        ForeignKey("opportunities.id", use_alter=True, name="trade_intents_opportunity_fk"),
    ),
    Column("side", Text, nullable=False),
    Column("effect", Text, nullable=False),
    Column("quantity", NUM, nullable=False),
    Column("order_type", Text, nullable=False),
    Column("limit_price", NUM, nullable=False),
    Column("price_band_low", NUM, nullable=False),
    Column("price_band_high", NUM, nullable=False),
    Column("max_slippage_bps", NUM, nullable=False),
    Column("time_in_force", Text, nullable=False),
    Column("management_plan", JSONB, nullable=False),
    Column("thesis", Text, nullable=False),
    Column("plain_summary", Text, nullable=False),
    Column("horizon", Text, nullable=False),
    Column("details", JSONB, nullable=False),
    Column("intent_expires_at", TS, nullable=False),
    Column("created_by_kind", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("status_reason", Text),
    Column("is_simulated", Boolean, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Index("trade_intents_status", "status"),
)

approval_packets = Table(
    "approval_packets",
    metadata,
    _id(),
    Column("intent_id", Uuid, ForeignKey("trade_intents.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("packet", JSONB, nullable=False),
    Column("packet_hash", Text, nullable=False, unique=True),
    Column("risk_check_run_id", Uuid, ForeignKey("risk_check_runs.id"), nullable=False),
    Column("policy_version", Integer, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    UniqueConstraint("intent_id", "version"),
)

approval_challenges = Table(
    "approval_challenges",
    metadata,
    _id(),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("intent_id", Uuid, ForeignKey("trade_intents.id"), nullable=False),
    Column("packet_id", Uuid, ForeignKey("approval_packets.id"), nullable=False),
    Column("packet_hash", Text, nullable=False),
    Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
    Column("channel", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("used_at", TS),
)

# ADR-030 (migration 0011): what the auto-trader may do on one simulated account.
trading_mandates = Table(
    "trading_mandates",
    metadata,
    _id(),
    Column("account_id", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("strategy_key", Text, nullable=False),
    Column("timeframe", Text, nullable=False),
    Column("instruments", ARRAY(Text), nullable=False),
    Column("max_loss_per_trade", NUM, nullable=False),
    Column("max_trades_per_day", Integer, nullable=False),
    Column("max_open_positions", Integer, nullable=False),
    Column("daily_loss_limit", NUM, nullable=False),
    Column("status", Text, nullable=False),
    Column("granted_by", Uuid, ForeignKey("users.id"), nullable=False),
    Column("granted_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("ended_at", TS),
    Column("ended_by", Text),
    Column("end_reason", Text),
    # Migration 0012: the money and care level it was granted from, and AUTO or ASK.
    Column("capital", NUM),
    Column("risk_pct", NUM),
    Column("mode", Text, nullable=False, server_default=text("'AUTO'::text")),
    # Migration 0013: the most it may lose in total under the mandate (NULL: ten trades' worth).
    Column("total_loss_limit", NUM),
    Index(
        "trading_mandates_one_active",
        "account_id",
        unique=True,
        postgresql_where=text("status = 'ACTIVE'::text"),
    ),
)

approvals = Table(
    "approvals",
    metadata,
    _id(),
    Column("intent_id", Uuid, ForeignKey("trade_intents.id"), nullable=False),
    Column("packet_id", Uuid, ForeignKey("approval_packets.id"), nullable=False, unique=True),
    Column("packet_hash", Text, nullable=False),
    Column("decision", Text, nullable=False),
    Column("operator_user_id", Uuid, ForeignKey("users.id"), nullable=False),
    Column("channel", Text, nullable=False),
    Column("nonce", Text, nullable=False, unique=True),
    Column("policy_version", Integer, nullable=False),
    Column("decided_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("status", Text, nullable=False),
    Column("consumed_at", TS),
    Column("consumed_order_id", Uuid),
    Column("status_reason", Text),
    Column("note", Text),
    Column("mandate_id", Uuid, ForeignKey("trading_mandates.id")),  # channel MANDATE only
    Index("approvals_intent", "intent_id"),
)

orders = Table(
    "orders",
    metadata,
    _id(),
    Column("intent_id", Uuid, ForeignKey("trade_intents.id"), nullable=False),
    Column("approval_id", Uuid, ForeignKey("approvals.id")),
    Column("parent_order_id", Uuid, ForeignKey("orders.id")),
    Column("purpose", Text, nullable=False),
    Column("account_id", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("instrument_id", Uuid, ForeignKey("instruments.id"), nullable=False),
    Column("client_order_id", Text, nullable=False, unique=True),
    Column("broker_order_id", Text),
    Column("side", Text, nullable=False),
    Column("order_type", Text, nullable=False),
    Column("quantity", NUM, nullable=False),
    Column("limit_price", NUM, nullable=False),
    Column("stop_price", NUM),
    Column("time_in_force", Text, nullable=False),
    Column("expires_at", TS),
    Column("status", Text, nullable=False),
    Column("status_version", Integer, nullable=False, server_default=text("1")),
    Column("filled_qty", NUM, nullable=False, server_default=text("0")),
    Column("avg_fill_price", NUM),
    Column("fees_paid", NUM, nullable=False, server_default=text("0")),
    Column("fee_currency", Text),
    Column("authority", Text, nullable=False),
    Column("reduce_only", Boolean, nullable=False),
    Column("is_simulated", Boolean, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("submitted_at", TS),
    Column("acknowledged_at", TS),
    Column("last_broker_sync_at", TS),
    Column("unknown_since", TS),
    Column("last_error", Text),
    Index(
        "orders_one_entry_per_intent",
        "intent_id",
        unique=True,
        postgresql_where=text("purpose = 'ENTRY'::text"),
    ),
    Index("orders_account_status", "account_id", "status"),
)

order_events = Table(
    "order_events",
    metadata,
    _id(),
    Column("order_id", Uuid, ForeignKey("orders.id"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("from_status", Text),
    Column("to_status", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("occurred_at", TS, nullable=False),
    UniqueConstraint("order_id", "seq"),
)

fills = Table(
    "fills",
    metadata,
    _id(),
    Column("order_id", Uuid, ForeignKey("orders.id"), nullable=False),
    Column("broker_fill_id", Text, nullable=False),
    Column("quantity", NUM, nullable=False),
    Column("price", NUM, nullable=False),
    Column("fee", NUM, nullable=False),
    Column("fee_currency", Text, nullable=False),
    Column("liquidity", Text),
    Column("realized_pnl", NUM),
    Column("filled_at", TS, nullable=False),
    Column("source", Text, nullable=False),
    Column("is_simulated", Boolean, nullable=False),
    Column("recorded_at", TS, nullable=False),
    UniqueConstraint("order_id", "broker_fill_id"),
    Index("fills_filled_at", "filled_at"),
)

reconciliation_runs = Table(
    "reconciliation_runs",
    metadata,
    _id(),
    Column("account_id", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("trigger", Text, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("finished_at", TS),
    Column("status", Text, nullable=False),
    Column("summary", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Index("reconciliation_runs_account", "account_id", "started_at"),
)

reconciliation_items = Table(
    "reconciliation_items",
    metadata,
    _id(),
    Column("run_id", Uuid, ForeignKey("reconciliation_runs.id"), nullable=False),
    Column("step", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("internal_value", Text),
    Column("broker_value", Text),
    Column("status", Text, nullable=False),
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Index("reconciliation_items_run", "run_id"),
)

recovery_checklists = Table(
    "recovery_checklists",
    metadata,
    _id(),
    Column("opened_at", TS, nullable=False),
    Column("opened_by", Text, nullable=False),
    Column("stop_reason", Text, nullable=False),
    Column("stop_version", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("completed_at", TS),
    Column("completed_by", Uuid, ForeignKey("users.id")),
    Column("resume_mode", Text),
    Column("resume_reason", Text),
    Column("evidence", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Index(
        "recovery_checklists_one_open",
        "status",
        unique=True,
        postgresql_where=text("status = 'OPEN'::text"),
    ),
)

paper_balances = Table(
    "paper_balances",
    metadata,
    Column("account_ref", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("asset", Text, nullable=False),
    Column("total", NUM, nullable=False),
    Column("hold", NUM, nullable=False, server_default=text("0")),
    Column("updated_at", TS, nullable=False),
    PrimaryKeyConstraint("account_ref", "asset"),
)

paper_orders = Table(
    "paper_orders",
    metadata,
    _id(),
    Column("account_ref", Uuid, ForeignKey("accounts.id"), nullable=False),
    Column("client_order_id", Text, nullable=False, unique=True),
    Column("venue_order_id", Text, nullable=False, unique=True),
    Column("instrument_key", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("order_type", Text, nullable=False),
    Column("quantity", NUM, nullable=False),
    Column("limit_price", NUM, nullable=False),
    Column("stop_price", NUM),
    Column("triggered", Boolean, nullable=False, server_default=text("false")),
    Column("time_in_force", Text, nullable=False),
    Column("expires_at", TS),
    Column("status", Text, nullable=False),
    Column("filled_qty", NUM, nullable=False, server_default=text("0")),
    Column("hold_asset", Text),
    Column("hold_amount", NUM, nullable=False, server_default=text("0")),
    Column("reject_reason", Text),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
)

paper_fills = Table(
    "paper_fills",
    metadata,
    _id(),
    Column("paper_order_id", Uuid, ForeignKey("paper_orders.id"), nullable=False),
    Column("venue_fill_id", Text, nullable=False, unique=True),
    Column("quantity", NUM, nullable=False),
    Column("price", NUM, nullable=False),
    Column("fee", NUM, nullable=False),
    Column("fee_asset", Text, nullable=False),
    Column("liquidity", Text, nullable=False),
    Column("filled_at", TS, nullable=False),
)

paper_faults = Table(
    "paper_faults",
    metadata,
    _id(),
    Column("fault", Text, nullable=False),
    Column("remaining", Integer, nullable=False),
    Column("created_at", TS, nullable=False),
)

post_trade_reviews = Table(
    "post_trade_reviews",
    metadata,
    _id(),
    Column("intent_id", Uuid, ForeignKey("trade_intents.id"), nullable=False, unique=True),
    Column("created_at", TS, nullable=False),
    Column("planned", JSONB, nullable=False),
    Column("actual", JSONB, nullable=False),
    Column("assessment", JSONB, nullable=False),
    Column("realized_pnl", NUM),
    Column("is_simulated", Boolean, nullable=False),
)

telegram_link_codes = Table(
    "telegram_link_codes",
    metadata,
    _id(),
    Column("code_hash", LargeBinary, nullable=False, unique=True),
    Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("used_at", TS),
)
