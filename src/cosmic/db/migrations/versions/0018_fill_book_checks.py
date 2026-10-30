"""Paper fills compared with the venue's real order book (S41, ADR-034 addendum).

- ``fill_book_checks``: for a paper fill on a venue whose public order book can be read
  (Binance), what the real book would have charged for the same size seconds later: the best
  price, the volume-weighted price through as many levels as the size takes (NULL when the
  book read was too thin), and when it was read: after the fill, and within a minute of it.
  Append-only, like the fills it checks.

The downgrade refuses while any check exists (nothing is deleted).

Revision ID: 0018_fill_book_checks
Revises: 0017_live_account_key
Create Date: 2026-10-30
"""

from cosmic.db.sqlsplit import run_script

revision = "0018_fill_book_checks"
down_revision = "0017_live_account_key"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    CREATE TABLE fill_book_checks (
      fill_id uuid PRIMARY KEY REFERENCES fills(id),
      account_id uuid NOT NULL REFERENCES accounts(id),
      venue text NOT NULL,
      symbol text NOT NULL,
      purpose text NOT NULL,
      side text NOT NULL CHECK (side IN ('BUY','SELL')),
      quantity numeric(38,18) NOT NULL CHECK (quantity > 0),
      paper_price numeric(38,18) NOT NULL CHECK (paper_price > 0),
      best_price numeric(38,18) NOT NULL CHECK (best_price > 0),
      book_price numeric(38,18) CHECK (book_price > 0),
      levels integer NOT NULL CHECK (levels >= 0),
      filled_at timestamptz NOT NULL,
      read_at timestamptz NOT NULL,
      source_key text NOT NULL,
      CHECK (read_at >= filled_at AND read_at <= filled_at + interval '1 minute')
    );
    CREATE INDEX fill_book_checks_read ON fill_book_checks (venue, read_at);
    CREATE TRIGGER fill_book_checks_immutable BEFORE UPDATE OR DELETE ON fill_book_checks
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON fill_book_checks FROM {app};
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM fill_book_checks) THEN
        RAISE EXCEPTION 'fill book checks exist; the schema before 0018 cannot hold them';
      END IF;
    END $$;
    DROP TABLE fill_book_checks;
    """)
