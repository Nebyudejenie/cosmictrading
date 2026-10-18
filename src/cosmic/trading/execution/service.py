"""Execution authority (V6 §1809–§1813, doc 08 §6, ADR-012).

The only code path that transmits orders. One leader at a time (Postgres advisory
lock). Every state claim is broker-grounded; ambiguity becomes UNKNOWN_ORDER_STATE and
is reconciled, never resubmitted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy import select, text

from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.redaction import scrub_text
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trade_intents
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.trading.brokers.registry import BrokerRegistry
from cosmic.trading.execution.common import (
    EXECUTION_LOCK_KEY,
    StepErrors,
    log,
)
from cosmic.trading.execution.entry import EntryExecution
from cosmic.trading.execution.protection import ProtectionManager
from cosmic.trading.execution.recovery import CancelAndRecovery
from cosmic.trading.execution.sync import OrderSync

STEP_ALERT_AFTER = 3  # leader cycles in a row
STEP_RISK = {
    "executed": "Approved trades are not being sent.",
    "released": "Approved exits are not being sent.",
    "synced": "Fills and order states from the venues are not being recorded.",
    "managed": "New fills may not get their protective orders.",
    "stopped": "During an emergency stop, entries open at a venue are not being cancelled.",
    "cancels_resent": "Unconfirmed cancels are not being sent again.",
}


class ExecutionService(EntryExecution, OrderSync, ProtectionManager, CancelAndRecovery):
    def __init__(
        self, db: Database, clock: Clock, settings: Settings, brokers: BrokerRegistry
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.brokers = brokers
        self.missing_since = {}
        self.step_failures: dict[str, int] = {}  # consecutive failing leader cycles per step
        self._steps_seen_ok: set[str] = set()

    @asynccontextmanager
    async def leadership(self) -> AsyncIterator[bool]:
        """Session-level advisory lock: exactly one execution authority at a time."""
        async with self.db.engine.connect() as conn:
            got = bool(
                await conn.scalar(text(f"SELECT pg_try_advisory_lock({EXECUTION_LOCK_KEY})"))
            )
            try:
                yield got
            finally:
                if got:
                    await conn.scalar(text(f"SELECT pg_advisory_unlock({EXECUTION_LOCK_KEY})"))
                await conn.commit()

    async def run_cycle(self) -> dict[str, int]:
        stats = {
            "executed": 0,
            "released": 0,
            "synced": 0,
            "managed": 0,
            "stopped": 0,
            "cancels_resent": 0,
        }
        async with self.leadership() as leader:
            if not leader:
                return stats
            steps: list[tuple[str, Callable[[], Awaitable[int]]]] = [
                ("executed", self._execute_approved),
                ("released", self.release_exits),
                ("synced", self.poll_active),
                ("managed", self.manage_positions),  # protection before the stop's cancels
                ("stopped", self.cancel_entries_under_stop),
                ("cancels_resent", self.follow_up_cancels),
            ]
            errors = StepErrors()
            for name, step in steps:  # each step runs even when an earlier one failed
                try:
                    stats[name] = await step()
                except Exception as exc:
                    errors.note(name, exc)
                    await self._step_failed(name, exc)
                else:
                    await self._step_succeeded(name)
            errors.raise_first()
        return stats

    async def _step_failed(self, name: str, exc: Exception) -> None:
        """A step that fails cycle after cycle raises its own incident, whatever the job
        scheduler records: a run that is not the leader returns normally, and job health counts
        it as a success."""
        streak = self.step_failures.get(name, 0) + 1
        self.step_failures[name] = streak
        if streak != STEP_ALERT_AFTER:
            return
        try:
            async with self.db.transaction() as conn:
                await raise_incident(
                    conn,
                    dedup_key=f"trading.cycle_step_failing:{name}",
                    category=Category.TRADING,
                    alert=Alert(
                        title=f"The execution cycle's '{name}' step keeps failing",
                        severity="CRITICAL",
                        what_happened=f"The step failed in {streak} execution cycles in a row: "
                        f"{type(exc).__name__}: {scrub_text(str(exc))[:200]}",
                        verified="The other steps of each cycle still ran.",
                        uncertain="Whether the cause is in our data, the configuration or a venue.",
                        risk_now=STEP_RISK.get(name, "Part of the execution cycle is not working."),
                        action_taken="The step is tried again every cycle; the other "
                        "steps keep running.",
                        owner_action="Check the worker log for 'execution_step_failed' and the "
                        "Operations page. A risk policy that fails its integrity check, or a "
                        "venue adapter missing in the worker, are common causes.",
                    ),
                    now=self.clock.now(),
                    detail={"step": name, "error": type(exc).__name__},
                )
        except Exception as alert_exc:  # the alert must not stop the cycle's other steps
            log.error("step_alert_failed", step=name, error=type(alert_exc).__name__)

    async def _step_succeeded(self, name: str) -> None:
        streak = self.step_failures.pop(name, 0)
        if streak < STEP_ALERT_AFTER and name in self._steps_seen_ok:
            return
        self._steps_seen_ok.add(name)  # first success in this process: close an older alert too
        try:
            async with self.db.transaction() as conn:
                await resolve_by_key(
                    conn,
                    dedup_key=f"trading.cycle_step_failing:{name}",
                    now=self.clock.now(),
                    resolution="the step succeeded again",
                )
        except Exception as exc:
            log.error("step_alert_resolve_failed", step=name, error=type(exc).__name__)

    async def _execute_approved(self) -> int:
        async with self.db.connect() as conn:
            approved = [
                r.id
                for r in await conn.execute(
                    select(trade_intents.c.id)
                    .where(trade_intents.c.status == "APPROVED")
                    .order_by(trade_intents.c.updated_at)
                )
            ]
        errors = StepErrors()
        for intent_id in approved:
            try:
                await self.execute_intent(intent_id)
            except Exception as exc:
                errors.note("execute_intent", exc, intent_id=str(intent_id))
        errors.raise_first()
        return len(approved)
