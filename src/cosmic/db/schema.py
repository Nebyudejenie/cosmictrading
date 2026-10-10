"""SQLAlchemy Core table definitions mirroring the Alembic migrations.

Migrations are the source of truth for DDL; ``tests/integration/test_migrations.py``
fails if this metadata drifts from the migrated database.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

metadata = MetaData()

TS = DateTime(timezone=True)
NUM = Numeric(38, 18)


def _id() -> Column[object]:
    return Column("id", Uuid, primary_key=True)


users = Table(
    "users",
    metadata,
    _id(),
    Column("username", Text, nullable=False, unique=True),
    Column("display_name", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("roles", ARRAY(Text), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("mfa_secret_enc", LargeBinary),
    Column("mfa_enabled", Boolean, nullable=False, server_default=text("false")),
    Column("mfa_last_step", BigInteger),
    Column("telegram_user_id", BigInteger, unique=True),
    Column("failed_logins", Integer, nullable=False, server_default=text("0")),
    Column("locked_until", TS),
    Column("password_changed_at", TS, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
)

sessions = Table(
    "sessions",
    metadata,
    _id(),
    Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("csrf_token", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("mfa_verified_at", TS),
    Column("revoked_at", TS),
    Column("revoked_reason", Text),
    Column("ip_address", Text),
    Column("user_agent", Text),
    Index("sessions_user_active", "user_id", postgresql_where=text("revoked_at IS NULL")),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("seq", BigInteger, primary_key=True, autoincrement=False),
    Column("id", Uuid, nullable=False, unique=True),
    Column("occurred_at", TS, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text),
    Column("action", Text, nullable=False),
    Column("entity_type", Text),
    Column("entity_id", Text),
    Column("correlation_id", Text),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("prev_hash", Text, nullable=False),
    Column("hash", Text, nullable=False, unique=True),
    Index("audit_events_entity", "entity_type", "entity_id"),
    Index("audit_events_occurred", "occurred_at"),
)

jobs = Table(
    "jobs",
    metadata,
    _id(),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("priority", SmallInteger, nullable=False, server_default=text("100")),
    Column("run_at", TS, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("3")),
    Column("locked_by", Text),
    Column("locked_until", TS),
    Column("idempotency_key", Text, unique=True),
    Column("last_error", Text),
    Column("created_at", TS, nullable=False),
    Column("started_at", TS),
    Column("finished_at", TS),
    Index("jobs_ready", "priority", "run_at", postgresql_where=text("status = 'QUEUED'")),
    Index("jobs_running", "locked_until", postgresql_where=text("status = 'RUNNING'")),
)

worker_heartbeats = Table(
    "worker_heartbeats",
    metadata,
    Column("worker_id", Text, primary_key=True),
    Column("process", Text, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("last_beat_at", TS, nullable=False),
    Column("info", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
)

incidents = Table(
    "incidents",
    metadata,
    _id(),
    Column("dedup_key", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("occurrences", Integer, nullable=False, server_default=text("1")),
    Column("opened_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("acknowledged_at", TS),
    Column("acknowledged_by", Uuid, ForeignKey("users.id")),
    Column("resolved_at", TS),
    Column("resolved_by", Uuid, ForeignKey("users.id")),
    Column("resolution", Text),
    Index(
        "incidents_open_dedup",
        "dedup_key",
        unique=True,
        postgresql_where=text("status <> 'RESOLVED'"),
    ),
)

notifications = Table(
    "notifications",
    metadata,
    _id(),
    Column("user_id", Uuid, ForeignKey("users.id")),
    Column("channel", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("dedup_key", Text, unique=True),
    Column("last_error", Text),
    Column("created_at", TS, nullable=False),
    Column("sent_at", TS),
    Index("notifications_pending", "created_at", postgresql_where=text("status = 'PENDING'")),
)

__all__ = ["NUM", "TS", "metadata"]
