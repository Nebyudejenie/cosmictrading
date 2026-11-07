"""A self-test replaced by the owner's newer START (S72): its own outcome, not a failure.

The owner pressed START three times within a minute on 2026-11-07; each press replaced the
mandate before it, and each replaced run was told as "SELF-TEST FAILED: the mandate ended
first". A run whose mandate the owner replaced now ends ``REPLACED`` (quietly: the new START
says what runs).

- ``gold_hfm_selftests.outcome`` may be ``REPLACED``.

The downgrade refuses while a replaced run exists.

Revision ID: 0025_selftest_replaced
Revises: 0024_ai_gold_opinions
Create Date: 2026-11-07
"""

from cosmic.db.sqlsplit import run_script

revision = "0025_selftest_replaced"
down_revision = "0024_ai_gold_opinions"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    ALTER TABLE gold_hfm_selftests DROP CONSTRAINT gold_hfm_selftests_outcome_check;
    ALTER TABLE gold_hfm_selftests ADD CONSTRAINT gold_hfm_selftests_outcome_check
      CHECK (outcome IS NULL OR outcome IN ('PASSED','FAILED','REPLACED'));
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM gold_hfm_selftests WHERE outcome = 'REPLACED') THEN
        RAISE EXCEPTION 'replaced self-test runs exist; the schema before 0025 cannot hold them';
      END IF;
    END $$;
    ALTER TABLE gold_hfm_selftests DROP CONSTRAINT gold_hfm_selftests_outcome_check;
    ALTER TABLE gold_hfm_selftests ADD CONSTRAINT gold_hfm_selftests_outcome_check
      CHECK (outcome IS NULL OR outcome IN ('PASSED','FAILED'));
    """
    )
