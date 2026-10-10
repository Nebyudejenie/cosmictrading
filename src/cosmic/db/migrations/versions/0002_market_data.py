"""Sources, provenance, instruments, bars/quotes, venue status, events, evidence.

Revision ID: 0002_market_data
Revises: 0001_foundation
Create Date: 2026-10-11
"""

from cosmic.db.sqlsplit import run_script

revision = "0002_market_data"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    CREATE TABLE sources (
      id uuid PRIMARY KEY,
      key text NOT NULL UNIQUE,
      name text NOT NULL,
      publisher text NOT NULL,
      jurisdiction text NOT NULL,
      language text NOT NULL,
      access_method text NOT NULL,
      base_url text NOT NULL,
      terms_url text,
      terms_status text NOT NULL CHECK (terms_status IN ('REVIEWED','UNVERIFIED','RESTRICTED')),
      licence_note text NOT NULL,
      data_class text NOT NULL CHECK (data_class IN ('MARKET_DATA','VENUE_STATUS','REFERENCE','DOCUMENT')),
      latency_class text NOT NULL CHECK (latency_class IN ('LIVE_STREAM','DELAYED_POLLING','END_OF_DAY')),
      update_frequency_seconds integer NOT NULL CHECK (update_frequency_seconds > 0),
      freshness_limit_seconds integer NOT NULL CHECK (freshness_limit_seconds > 0),
      timezone text NOT NULL,
      currency_unit text,
      known_bias text NOT NULL,
      missingness_note text NOT NULL,
      quality_checks text[] NOT NULL,
      owner text NOT NULL,
      fallback_source_key text,
      enabled boolean NOT NULL,
      last_success_at timestamptz,
      last_error_at timestamptz,
      last_error text,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL
    );

    CREATE TABLE fetch_runs (
      id uuid PRIMARY KEY,
      source_id uuid NOT NULL REFERENCES sources(id),
      kind text NOT NULL,
      subject text,
      started_at timestamptz NOT NULL,
      finished_at timestamptz,
      status text NOT NULL CHECK (status IN ('RUNNING','SUCCESS','PARTIAL','FAILED')),
      http_status integer,
      records_received integer NOT NULL DEFAULT 0,
      records_new integer NOT NULL DEFAULT 0,
      records_updated integer NOT NULL DEFAULT 0,
      records_rejected integer NOT NULL DEFAULT 0,
      error text
    );
    CREATE INDEX fetch_runs_source_time ON fetch_runs (source_id, started_at);

    CREATE TABLE data_quality_results (
      id uuid PRIMARY KEY,
      fetch_run_id uuid NOT NULL REFERENCES fetch_runs(id),
      check_name text NOT NULL,
      status text NOT NULL CHECK (status IN ('PASS','WARN','FAIL')),
      detail jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at timestamptz NOT NULL
    );
    CREATE INDEX data_quality_run ON data_quality_results (fetch_run_id);

    CREATE TABLE venues (
      id uuid PRIMARY KEY,
      key text NOT NULL UNIQUE,
      name text NOT NULL,
      kind text NOT NULL CHECK (kind IN ('EXCHANGE','SIMULATED')),
      timezone text NOT NULL,
      session text NOT NULL,
      status_source_key text,
      created_at timestamptz NOT NULL
    );

    CREATE TABLE assets (
      id uuid PRIMARY KEY,
      symbol text NOT NULL UNIQUE,
      name text NOT NULL,
      kind text NOT NULL CHECK (kind IN ('CRYPTO','FIAT')),
      display_decimals integer NOT NULL
    );

    CREATE TABLE instruments (
      id uuid PRIMARY KEY,
      canonical_key text NOT NULL UNIQUE,
      pair_key text NOT NULL,
      venue_id uuid NOT NULL REFERENCES venues(id),
      base_asset_id uuid NOT NULL REFERENCES assets(id),
      quote_asset_id uuid NOT NULL REFERENCES assets(id),
      asset_class text NOT NULL CHECK (asset_class IN ('CRYPTO_SPOT')),
      tick_size numeric(38,18),
      lot_size numeric(38,18),
      min_qty numeric(38,18),
      max_qty numeric(38,18),
      min_notional numeric(38,18),
      price_decimals integer,
      qty_decimals integer,
      contract_multiplier numeric(38,18) NOT NULL DEFAULT 1,
      settlement text NOT NULL DEFAULT 'SPOT',
      session text NOT NULL DEFAULT '24x7',
      timezone text NOT NULL DEFAULT 'UTC',
      status text NOT NULL CHECK (status IN ('ONLINE','LIMIT_ONLY','CANCEL_ONLY','POST_ONLY','HALTED','DELISTED','UNKNOWN')),
      tradable boolean NOT NULL DEFAULT false,
      metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
      metadata_source_id uuid REFERENCES sources(id),
      metadata_fetch_run_id uuid REFERENCES fetch_runs(id),
      metadata_fetched_at timestamptz,
      updated_at timestamptz NOT NULL,
      CHECK (tick_size IS NULL OR tick_size > 0),
      CHECK (lot_size IS NULL OR lot_size > 0)
    );
    CREATE INDEX instruments_pair ON instruments (pair_key);

    CREATE TABLE instrument_listings (
      id uuid PRIMARY KEY,
      instrument_id uuid NOT NULL REFERENCES instruments(id),
      source_id uuid NOT NULL REFERENCES sources(id),
      source_symbol text NOT NULL,
      UNIQUE (source_id, source_symbol),
      UNIQUE (instrument_id, source_id)
    );

    CREATE TABLE bars (
      instrument_id uuid NOT NULL REFERENCES instruments(id),
      source_id uuid NOT NULL REFERENCES sources(id),
      timeframe text NOT NULL CHECK (timeframe IN ('1m','5m','15m','1h','6h','1d')),
      open_time timestamptz NOT NULL,
      open numeric(38,18) NOT NULL,
      high numeric(38,18) NOT NULL,
      low numeric(38,18) NOT NULL,
      close numeric(38,18) NOT NULL,
      volume numeric(38,18) NOT NULL,
      is_complete boolean NOT NULL,
      fetch_run_id uuid NOT NULL REFERENCES fetch_runs(id),
      ingested_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL,
      PRIMARY KEY (instrument_id, source_id, timeframe, open_time),
      CHECK (low > 0 AND low <= open AND low <= close AND high >= open AND high >= close AND volume >= 0)
    );

    CREATE TABLE quotes (
      id uuid PRIMARY KEY,
      instrument_id uuid NOT NULL REFERENCES instruments(id),
      source_id uuid NOT NULL REFERENCES sources(id),
      bid numeric(38,18),
      ask numeric(38,18),
      last numeric(38,18),
      source_ts timestamptz NOT NULL,
      ingested_at timestamptz NOT NULL,
      fetch_run_id uuid NOT NULL REFERENCES fetch_runs(id),
      UNIQUE (instrument_id, source_id, source_ts),
      CHECK (bid IS NULL OR ask IS NULL OR bid <= ask),
      CHECK (coalesce(bid, 1) > 0 AND coalesce(ask, 1) > 0 AND coalesce(last, 1) > 0)
    );
    CREATE INDEX quotes_latest ON quotes (instrument_id, source_id, source_ts);

    CREATE TABLE venue_status (
      id uuid PRIMARY KEY,
      venue_id uuid NOT NULL REFERENCES venues(id),
      source_id uuid NOT NULL REFERENCES sources(id),
      status text NOT NULL CHECK (status IN ('OPERATIONAL','DEGRADED','MAINTENANCE','UNKNOWN')),
      indicator text NOT NULL,
      description text NOT NULL,
      source_ts timestamptz,
      ingested_at timestamptz NOT NULL,
      fetch_run_id uuid REFERENCES fetch_runs(id)
    );
    CREATE INDEX venue_status_latest ON venue_status (venue_id, ingested_at);

    CREATE TABLE market_events (
      id uuid PRIMARY KEY,
      instrument_id uuid REFERENCES instruments(id),
      kind text NOT NULL,
      detected_at timestamptz NOT NULL,
      bar_time timestamptz,
      detail jsonb NOT NULL DEFAULT '{}'::jsonb,
      dedup_key text NOT NULL UNIQUE,
      status text NOT NULL CHECK (status IN ('NEW','PROCESSED','IGNORED'))
    );
    CREATE INDEX market_events_new ON market_events (detected_at) WHERE status = 'NEW';

    CREATE TABLE documents (
      id uuid PRIMARY KEY,
      source_id uuid REFERENCES sources(id),
      url text NOT NULL,
      title text,
      retrieved_at timestamptz NOT NULL,
      content_hash text NOT NULL,
      content_type text,
      content_text text NOT NULL,
      classification text NOT NULL DEFAULT 'UNTRUSTED' CHECK (classification IN ('UNTRUSTED')),
      UNIQUE (url, content_hash)
    );

    CREATE TABLE claims (
      id uuid PRIMARY KEY,
      mission_id uuid,
      subject_type text,
      subject_id text,
      statement text NOT NULL,
      claim_type text NOT NULL CHECK (claim_type IN ('NUMERIC','QUALITATIVE')),
      metric text,
      value numeric(38,18),
      unit text,
      as_of timestamptz,
      status text NOT NULL CHECK (status IN ('VERIFIED','INFERRED','UNVERIFIED','CONTRADICTED')),
      verification_method text,
      verified_at timestamptz,
      created_by text NOT NULL,
      created_at timestamptz NOT NULL
    );
    CREATE INDEX claims_mission ON claims (mission_id);

    CREATE TABLE evidence_links (
      id uuid PRIMARY KEY,
      claim_id uuid NOT NULL REFERENCES claims(id),
      evidence_type text NOT NULL CHECK (evidence_type IN
        ('BAR','QUOTE','DOCUMENT','FETCH_RUN','BACKTEST','INDICATOR','MARKET_EVENT','VENUE_STATUS')),
      evidence_ref text NOT NULL,
      source_ts timestamptz,
      note text
    );
    CREATE INDEX evidence_links_claim ON evidence_links (claim_id);

    CREATE TABLE contradictions (
      id uuid PRIMARY KEY,
      claim_id uuid NOT NULL REFERENCES claims(id),
      other_claim_id uuid REFERENCES claims(id),
      description text NOT NULL,
      detected_at timestamptz NOT NULL,
      status text NOT NULL CHECK (status IN ('OPEN','RESOLVED'))
    );
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP TABLE contradictions;
    DROP TABLE evidence_links;
    DROP TABLE claims;
    DROP TABLE documents;
    DROP TABLE market_events;
    DROP TABLE venue_status;
    DROP TABLE quotes;
    DROP TABLE bars;
    DROP TABLE instrument_listings;
    DROP TABLE instruments;
    DROP TABLE assets;
    DROP TABLE venues;
    DROP TABLE data_quality_results;
    DROP TABLE fetch_runs;
    DROP TABLE sources;
    """)
