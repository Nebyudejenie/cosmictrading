"""Core tables: sources, provenance, instruments, market data, evidence (migration 0002)."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from cosmic.db.schema import NUM, TS, metadata

sources = Table(
    "sources",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("key", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("publisher", Text, nullable=False),
    Column("jurisdiction", Text, nullable=False),
    Column("language", Text, nullable=False),
    Column("access_method", Text, nullable=False),
    Column("base_url", Text, nullable=False),
    Column("terms_url", Text),
    Column("terms_status", Text, nullable=False),
    Column("licence_note", Text, nullable=False),
    Column("data_class", Text, nullable=False),
    Column("latency_class", Text, nullable=False),
    Column("update_frequency_seconds", Integer, nullable=False),
    Column("freshness_limit_seconds", Integer, nullable=False),
    Column("timezone", Text, nullable=False),
    Column("currency_unit", Text),
    Column("known_bias", Text, nullable=False),
    Column("missingness_note", Text, nullable=False),
    Column("quality_checks", ARRAY(Text), nullable=False),
    Column("owner", Text, nullable=False),
    Column("fallback_source_key", Text),
    Column("enabled", Boolean, nullable=False),
    Column("last_success_at", TS),
    Column("last_error_at", TS),
    Column("last_error", Text),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
)

fetch_runs = Table(
    "fetch_runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("source_id", Uuid, ForeignKey("sources.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("subject", Text),
    Column("started_at", TS, nullable=False),
    Column("finished_at", TS),
    Column("status", Text, nullable=False),
    Column("http_status", Integer),
    Column("records_received", Integer, nullable=False, server_default=text("0")),
    Column("records_new", Integer, nullable=False, server_default=text("0")),
    Column("records_updated", Integer, nullable=False, server_default=text("0")),
    Column("records_rejected", Integer, nullable=False, server_default=text("0")),
    Column("error", Text),
    Index("fetch_runs_source_time", "source_id", "started_at"),
)

data_quality_results = Table(
    "data_quality_results",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("fetch_run_id", Uuid, ForeignKey("fetch_runs.id"), nullable=False),
    Column("check_name", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", TS, nullable=False),
    Index("data_quality_run", "fetch_run_id"),
)

venues = Table(
    "venues",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("key", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("timezone", Text, nullable=False),
    Column("session", Text, nullable=False),
    Column("status_source_key", Text),
    Column("created_at", TS, nullable=False),
)

assets = Table(
    "assets",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("symbol", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("display_decimals", Integer, nullable=False),
)

instruments = Table(
    "instruments",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("canonical_key", Text, nullable=False, unique=True),
    Column("pair_key", Text, nullable=False),
    Column("venue_id", Uuid, ForeignKey("venues.id"), nullable=False),
    Column("base_asset_id", Uuid, ForeignKey("assets.id"), nullable=False),
    Column("quote_asset_id", Uuid, ForeignKey("assets.id"), nullable=False),
    Column("asset_class", Text, nullable=False),
    Column("tick_size", NUM),
    Column("lot_size", NUM),
    Column("min_qty", NUM),
    Column("max_qty", NUM),
    Column("min_notional", NUM),
    Column("price_decimals", Integer),
    Column("qty_decimals", Integer),
    Column("contract_multiplier", NUM, nullable=False, server_default=text("1")),
    Column("settlement", Text, nullable=False, server_default=text("'SPOT'::text")),
    Column("session", Text, nullable=False, server_default=text("'24x7'::text")),
    Column("timezone", Text, nullable=False, server_default=text("'UTC'::text")),
    Column("status", Text, nullable=False),
    Column("tradable", Boolean, nullable=False, server_default=text("false")),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("metadata_source_id", Uuid, ForeignKey("sources.id")),
    Column("metadata_fetch_run_id", Uuid, ForeignKey("fetch_runs.id")),
    Column("metadata_fetched_at", TS),
    Column("updated_at", TS, nullable=False),
    Index("instruments_pair", "pair_key"),
)

instrument_listings = Table(
    "instrument_listings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instrument_id", Uuid, ForeignKey("instruments.id"), nullable=False),
    Column("source_id", Uuid, ForeignKey("sources.id"), nullable=False),
    Column("source_symbol", Text, nullable=False),
    UniqueConstraint("source_id", "source_symbol"),
    UniqueConstraint("instrument_id", "source_id"),
)

bars = Table(
    "bars",
    metadata,
    Column("instrument_id", Uuid, ForeignKey("instruments.id"), nullable=False),
    Column("source_id", Uuid, ForeignKey("sources.id"), nullable=False),
    Column("timeframe", Text, nullable=False),
    Column("open_time", TS, nullable=False),
    Column("open", NUM, nullable=False),
    Column("high", NUM, nullable=False),
    Column("low", NUM, nullable=False),
    Column("close", NUM, nullable=False),
    Column("volume", NUM, nullable=False),
    Column("is_complete", Boolean, nullable=False),
    Column("fetch_run_id", Uuid, ForeignKey("fetch_runs.id"), nullable=False),
    Column("ingested_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    PrimaryKeyConstraint("instrument_id", "source_id", "timeframe", "open_time"),
)

quotes = Table(
    "quotes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instrument_id", Uuid, ForeignKey("instruments.id"), nullable=False),
    Column("source_id", Uuid, ForeignKey("sources.id"), nullable=False),
    Column("bid", NUM),
    Column("ask", NUM),
    Column("last", NUM),
    Column("source_ts", TS, nullable=False),
    Column("ingested_at", TS, nullable=False),
    Column("fetch_run_id", Uuid, ForeignKey("fetch_runs.id"), nullable=False),
    UniqueConstraint("instrument_id", "source_id", "source_ts"),
    Index("quotes_latest", "instrument_id", "source_id", "source_ts"),
)

venue_status = Table(
    "venue_status",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("venue_id", Uuid, ForeignKey("venues.id"), nullable=False),
    Column("source_id", Uuid, ForeignKey("sources.id"), nullable=False),
    Column("status", Text, nullable=False),
    Column("indicator", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("source_ts", TS),
    Column("ingested_at", TS, nullable=False),
    Column("fetch_run_id", Uuid, ForeignKey("fetch_runs.id")),
    Index("venue_status_latest", "venue_id", "ingested_at"),
)

market_events = Table(
    "market_events",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instrument_id", Uuid, ForeignKey("instruments.id")),
    Column("kind", Text, nullable=False),
    Column("detected_at", TS, nullable=False),
    Column("bar_time", TS),
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("dedup_key", Text, nullable=False, unique=True),
    Column("status", Text, nullable=False),
    Index("market_events_new", "detected_at", postgresql_where=text("status = 'NEW'")),
)

documents = Table(
    "documents",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("source_id", Uuid, ForeignKey("sources.id")),
    Column("url", Text, nullable=False),
    Column("title", Text),
    Column("retrieved_at", TS, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("content_type", Text),
    Column("content_text", Text, nullable=False),
    Column("classification", Text, nullable=False, server_default=text("'UNTRUSTED'::text")),
    UniqueConstraint("url", "content_hash"),
)

claims = Table(
    "claims",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("mission_id", Uuid, ForeignKey("missions.id", use_alter=True, name="claims_mission_fk")),
    Column("subject_type", Text),
    Column("subject_id", Text),
    Column("statement", Text, nullable=False),
    Column("claim_type", Text, nullable=False),
    Column("metric", Text),
    Column("value", NUM),
    Column("unit", Text),
    Column("as_of", TS),
    Column("status", Text, nullable=False),
    Column("verification_method", Text),
    Column("verified_at", TS),
    Column("created_by", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Index("claims_mission", "mission_id"),
)

evidence_links = Table(
    "evidence_links",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("claim_id", Uuid, ForeignKey("claims.id"), nullable=False),
    Column("evidence_type", Text, nullable=False),
    Column("evidence_ref", Text, nullable=False),
    Column("source_ts", TS),
    Column("note", Text),
    Index("evidence_links_claim", "claim_id"),
)

contradictions = Table(
    "contradictions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("claim_id", Uuid, ForeignKey("claims.id"), nullable=False),
    Column("other_claim_id", Uuid, ForeignKey("claims.id")),
    Column("description", Text, nullable=False),
    Column("detected_at", TS, nullable=False),
    Column("status", Text, nullable=False),
)
