"""Core tables: AI gateway (migration 0005)."""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Index, Integer, Table, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB

from cosmic.db.schema import NUM, TS, metadata

ai_provider_attestations = Table(
    "ai_provider_attestations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider_key", Text, nullable=False),
    Column("statement", Text, nullable=False),
    Column("evidence_url", Text, nullable=False),
    Column("rpm_limit", Integer),
    Column("rpd_limit", Integer),
    Column("attested_by", Uuid, ForeignKey("users.id"), nullable=False),
    Column("attested_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("revoked_at", TS),
    Index("ai_attestations_provider", "provider_key", "attested_at"),
)

ai_model_approvals = Table(
    "ai_model_approvals",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider_key", Text, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("tier", Text, nullable=False),
    Column("context_limit", Integer),
    Column("evidence_url", Text, nullable=False),
    Column("approved_by", Uuid, ForeignKey("users.id"), nullable=False),
    Column("approved_at", TS, nullable=False),
    Column("review_expires_at", TS, nullable=False),
    Column("revoked_at", TS),
    Column("price_checked_at", TS),
    Column("price_check_result", Text),
    Index(
        "ai_model_approvals_active",
        "provider_key",
        "model_id",
        unique=True,
        postgresql_where=text("revoked_at IS NULL"),
    ),
)

ai_provider_state = Table(
    "ai_provider_state",
    metadata,
    Column("provider_key", Text, primary_key=True),
    Column("circuit", Text, nullable=False),
    Column("consecutive_failures", Integer, nullable=False, server_default=text("0")),
    Column("opened_until", TS),
    Column("rate_limited_until", TS),
    Column("last_success_at", TS),
    Column("last_error_at", TS),
    Column("last_error", Text),
    Column("disabled_reason", Text),
    Column("updated_at", TS, nullable=False),
)

ai_usage = Table(
    "ai_usage",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider_key", Text, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("task_type", Text, nullable=False),
    Column("role_key", Text),
    Column("request_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("http_status", Integer),
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("latency_ms", Integer),
    Column("reported_cost", NUM),
    Column("error", Text),
    Column("created_at", TS, nullable=False),
    Index("ai_usage_provider_time", "provider_key", "created_at"),
)

ai_cache = Table(
    "ai_cache",
    metadata,
    Column("cache_key", Text, primary_key=True),
    Column("task_type", Text, nullable=False),
    Column("response", JSONB, nullable=False),
    Column("provider_key", Text, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Index("ai_cache_expiry", "expires_at"),
)
