"""Actionable alerts (doc 14 §3, TRD-33).

Every alert answers the same six questions so the owner never has to guess: what
happened, what is verified, what is uncertain, the risk right now, what the system
already did, and what the owner must do. Empty answers are rejected — write
"nothing" or "none" explicitly instead.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ValidationFailedError
from cosmic.notify.outbox import notify_owners

Severity = Literal["INFO", "WARNING", "CRITICAL"]
FIELDS = ("what_happened", "verified", "uncertain", "risk_now", "action_taken", "owner_action")
LABELS = {
    "what_happened": "What happened",
    "verified": "Verified",
    "uncertain": "Uncertain",
    "risk_now": "Risk now",
    "action_taken": "Already done",
    "owner_action": "What you should do",
}


@dataclass(frozen=True)
class Alert:
    title: str
    severity: Severity
    what_happened: str
    verified: str
    uncertain: str
    risk_now: str
    action_taken: str
    owner_action: str

    def __post_init__(self) -> None:
        missing = [f for f in ("title", *FIELDS) if not str(getattr(self, f)).strip()]
        if missing:
            raise ValidationFailedError(f"alert is missing: {', '.join(missing)}")
        if self.severity not in ("INFO", "WARNING", "CRITICAL"):
            raise ValidationFailedError(f"unknown alert severity {self.severity}")

    def payload(self) -> dict[str, str]:
        return {k: str(v) for k, v in asdict(self).items()}

    def text(self) -> str:
        """Plain text for Telegram and logs (no markup)."""
        lines = [f"{self.severity}: {self.title}"]
        lines += [f"{LABELS[f]}: {getattr(self, f)}" for f in FIELDS]
        return "\n".join(lines)


def alert_from_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    """The alert stored in a notification payload, if it is an alert."""
    alert = payload.get("alert")
    if isinstance(alert, dict) and all(f in alert for f in FIELDS):
        return {str(k): str(v) for k, v in alert.items()}
    return None


async def notify_alert(
    conn: AsyncConnection,
    *,
    kind: str,
    alert: Alert,
    now: datetime,
    dedup_key: str | None = None,
    context: dict[str, Any] | None = None,
) -> int:
    """Send an alert to every owner (portal always; Telegram when linked)."""
    return await notify_owners(
        conn,
        kind=kind,
        payload={**(context or {}), "alert": alert.payload()},
        now=now,
        dedup_key=dedup_key,
    )
