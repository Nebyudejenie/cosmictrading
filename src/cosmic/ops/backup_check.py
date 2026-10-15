"""When backups need the owner's attention (ADR-019, runbook §5). Pure: report in, alerts out.

Nightly backups run at 02:30 UTC, and every deploy takes one; the owner runs a restore drill
monthly from the workstation (the private key never lives on the server). The worker reads the
backup directory read-only and raises one incident per problem; each resolves by itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from cosmic.notify.alerts import Alert
from cosmic.ops.backups import BackupReport

OVERDUE_AFTER = timedelta(hours=26)  # the nightly backup plus slack
CRITICAL_AFTER = timedelta(hours=50)  # two nights missed
DRILL_DUE_AFTER = timedelta(days=35)  # monthly drill
KEYS = ("ops.backup_overdue", "ops.backup_incomplete", "ops.restore_drill_due")


def _hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.0f} h"


def backup_alerts(report: BackupReport, now: datetime) -> dict[str, Alert]:
    """Incident key → alert, for each problem found; keys not returned are healthy."""
    out: dict[str, Alert] = {}
    newest = report.newest
    if report.error is not None:
        out["ops.backup_overdue"] = Alert(
            title="Backups cannot be checked",
            severity="WARNING",
            what_happened=f"The worker cannot read {report.directory}: {report.error}.",
            verified="Only that the directory is not readable by the worker.",
            uncertain="Whether the backups themselves are fine.",
            risk_now="Backups may be fine, but nothing confirms that they are written.",
            action_taken="Incident opened; nothing was changed.",
            owner_action="Give the worker read access to the backup directory (runbook §8) "
            "and check backup.log.",
        )
    elif newest is None:
        out["ops.backup_overdue"] = Alert(
            title="No backup found",
            severity="CRITICAL",
            what_happened=f"The worker found no backup set in {report.directory}.",
            verified="Read just now by the worker (read-only).",
            uncertain="Whether backups are written elsewhere or the nightly job stopped.",
            risk_now="If the database is lost now, there is nothing to restore from.",
            action_taken="Incident opened; nothing was changed.",
            owner_action="Check the backup job's log (backup.log) and run a backup now "
            "(runbook §5).",
        )
    else:
        age = now - newest.created_at
        if age > OVERDUE_AFTER:
            out["ops.backup_overdue"] = Alert(
                title=f"Newest backup is {_hours(age)} old",
                severity="CRITICAL" if age > CRITICAL_AFTER else "WARNING",
                what_happened=f"The newest backup is {newest.stem}; the nightly backup should "
                "have written a newer one.",
                verified="Read just now by the worker from the backup directory.",
                uncertain="Why the nightly backup did not run or did not finish.",
                risk_now=f"A restore today would lose up to {_hours(age)} of records.",
                action_taken="Incident opened; nothing was changed.",
                owner_action="Check backup.log and the cron entry, then run a backup now "
                "(runbook §5).",
            )
        if newest.problems:
            out["ops.backup_incomplete"] = Alert(
                title="Newest backup set is incomplete",
                severity="CRITICAL",
                what_happened=f"{newest.stem}: " + "; ".join(newest.problems) + ".",
                verified="File names and sizes compared with the set's manifest just now.",
                uncertain="Whether the set can still be restored.",
                risk_now="The newest backup may not restore; older sets may still.",
                action_taken="Incident opened; nothing was deleted or changed.",
                owner_action="Run a backup now, then a restore drill on it (runbook §5).",
            )
    drill = report.drill
    if drill is None or not drill.passed or now - drill.drilled_at > DRILL_DUE_AFTER:
        failed = drill is not None and not drill.passed
        last = "never recorded" if drill is None else f"{drill.drilled_at:%Y-%m-%d} on {drill.stem}"
        out["ops.restore_drill_due"] = Alert(
            title="The last restore drill FAILED" if failed else "Restore drill due",
            severity="CRITICAL" if failed else "WARNING",
            what_happened=f"Last restore drill: {last}"
            + (" — it failed." if failed else "; it is done monthly."),
            verified="Read from restore-drills.jsonl in the backup directory.",
            uncertain="Whether the newest backups restore.",
            risk_now="A backup that has never been restored is not known to work.",
            action_taken="Incident opened; nothing was changed.",
            owner_action="Run scripts/drill-staging.sh from the workstation (runbook §5).",
        )
    return out
