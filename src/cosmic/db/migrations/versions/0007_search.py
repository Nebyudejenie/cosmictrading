"""Full-text search over research claims, mission reports and proposal reasoning (DATA-10).

Revision ID: 0007_search
Revises: 0006_research
Create Date: 2026-10-12
"""

from cosmic.db.sqlsplit import run_script

revision = "0007_search"
down_revision = "0006_research"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    CREATE INDEX claims_fts ON claims
      USING gin (to_tsvector('english', statement));
    CREATE INDEX artifacts_report_fts ON artifacts
      USING gin (jsonb_to_tsvector('english', content, '["string"]'))
      WHERE kind = 'report';
    CREATE INDEX trade_intents_fts ON trade_intents
      USING gin (to_tsvector('english', plain_summary || ' ' || thesis));
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP INDEX trade_intents_fts;
    DROP INDEX artifacts_report_fts;
    DROP INDEX claims_fts;
    """)
