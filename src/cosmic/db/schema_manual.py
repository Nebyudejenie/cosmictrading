"""Manual Trading (migration 0038, S113): the owner's risk rules and trade journal."""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
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

manual_risk_settings = Table(
    "manual_risk_settings",
    metadata,
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("params", JSONB, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    CheckConstraint("version > 0"),
)

manual_journal = Table(
    "manual_journal",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("owner_id", Uuid, ForeignKey("users.id"), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("kind", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("side", Text),
    Column("entry", NUM),
    Column("stop", NUM),
    Column("target", NUM),
    Column("lots", NUM),
    Column("risk_cash", NUM),
    Column("risk_pct", NUM),
    Column("reward_cash", NUM),
    Column("currency", Text),
    Column("account_fingerprint", Text),
    Column("insight", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("checks", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("note", Text),
    Column("request_key", Text, nullable=False),
    UniqueConstraint("request_key", name="manual_journal_request_key"),
    Index("manual_journal_created", "created_at"),
)
