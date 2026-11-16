"""What is asked many times a second is found at once (S92).

Measured in production on 2026-11-16 (537,000 quotes, 2,270 bridge commands), while checking
the server after bridge 9:

- **The latest quote of an instrument** (``market.data.latest_quote``) read every quote stored
  and sorted them: 0.19 to 0.37 s a call, and of everything the database was doing, four parts
  in five. Both indexes on ``quotes`` began with (instrument_id, source_id), so "the newest by
  time, whatever its source" could be read from neither. It is asked for every order's risk
  checks, for every open position and resting paper order in every trading cycle, and by most
  pages; it grew slower with every quote stored.
- **"Does an order wait for this bridge?"**, asked every 20 ms by each held request of the
  fast lane (three of them since bridge 9), read every command ever sent: the statement is
  prepared once, and with its status a parameter the planner's plan for "any status" is to
  read the table.

So:

- ``quotes_latest`` is rebuilt on (instrument_id, source_ts). Its former columns
  (instrument_id, source_id, source_ts) are exactly the unique constraint's own index, which
  stays: nothing loses an index.
- ``bridge_commands_pending``: the commands still waiting, by bridge and time. Nearly always
  empty. The statements that look for them name the status in their text (see
  ``bridge_sync.PENDING``), so this index serves them whatever the plan.

Revision ID: 0034_hot_lookups
Revises: 0033_bars_unfinished
Create Date: 2026-11-16
"""

from cosmic.db.sqlsplit import run_script

revision = "0034_hot_lookups"
down_revision = "0033_bars_unfinished"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP INDEX quotes_latest;
    CREATE INDEX quotes_latest ON quotes (instrument_id, source_ts);
    CREATE INDEX bridge_commands_pending ON bridge_commands (bridge, created_at)
      WHERE status = 'PENDING';
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP INDEX bridge_commands_pending;
    DROP INDEX quotes_latest;
    CREATE INDEX quotes_latest ON quotes (instrument_id, source_id, source_ts);
    """)
