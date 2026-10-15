"""Research missions (WF-08, doc 06 §7): detect → data check → quant → narrative (AI, optional)
→ proposal or NO_TRADE → report → archive. Every task output is reviewed before it counts.

AI is used only for the narrative of entry signals (cost control). When no free AI route is
available the mission waits and then expires without a proposal (AI-22). Numbers in AI
output are re-verified by code; unverifiable statements are stored as INFERRED.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.ai.gateway import AIGateway, AITask
from cosmic.ai.safety import DataClass, UntrustedItem
from cosmic.audit.service import Actor, record
from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.core.labels import DataLabel
from cosmic.core.logs import get_logger
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_market import market_events
from cosmic.db.schema_research import artifacts, missions, opportunities, tasks
from cosmic.evidence.service import add_claim, mission_claims, verify_numeric
from cosmic.market import data as md
from cosmic.market import indicators as ind
from cosmic.market.regime import classify
from cosmic.research import opportunities as opp
from cosmic.research.registry import ensure_strategy, run_and_store
from cosmic.research.strategy import TrendSmaAtr
from cosmic.trading.accounts import get_account, paper_account_for_venue
from cosmic.trading.context_builder import account_state
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import Candidate, create_candidate
from cosmic.trading.sizing import size_long
from cosmic.workforce import tasks as engine
from cosmic.workforce.priority import narrative_priority
from cosmic.workforce.tools import Toolbox

log = get_logger(__name__)
MISSION_TTL = timedelta(hours=2)
MIN_BARS = 200
WORKER = "research-worker"


class NarrativeClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(max_length=300)
    metric: str | None = Field(default=None, max_length=40)
    value: float | None = None


class NarrativeScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=60)
    description: str = Field(min_length=5, max_length=400)


class Narrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thesis: str = Field(min_length=20, max_length=1500)
    plain_summary: str = Field(min_length=20, max_length=400)
    counterarguments: list[str] = Field(min_length=1, max_length=6)
    red_team: str = Field(min_length=10, max_length=800)
    scenarios: list[NarrativeScenario] = Field(min_length=1, max_length=4)
    invalidation: list[str] = Field(min_length=1, max_length=4)
    claims: list[NarrativeClaim] = Field(default_factory=list, max_length=10)


@dataclass
class MissionDeps:
    db: Database
    clock: Clock
    settings: Settings
    ai: AIGateway | None


class MissionRunner:
    def __init__(self, deps: MissionDeps) -> None:
        self.d = deps
        self.strategy = TrendSmaAtr()

    # ---- opening ------------------------------------------------------------------------

    async def open_from_events(self) -> int:
        now = self.d.clock.now()
        opened = 0
        async with self.d.db.transaction() as conn:
            rows = list(
                await conn.execute(
                    select(market_events)
                    .where(market_events.c.status == "NEW")
                    .order_by(market_events.c.detected_at)
                    .limit(20)
                    .with_for_update(skip_locked=True)
                )
            )
            for ev in rows:
                if ev.instrument_id is None:
                    await conn.execute(
                        update(market_events)
                        .where(market_events.c.id == ev.id)
                        .values(status="IGNORED")
                    )
                    continue
                inst = await md.instrument_by_id(conn, ev.instrument_id)
                if not inst.tradable:
                    await conn.execute(
                        update(market_events)
                        .where(market_events.c.id == ev.id)
                        .values(status="IGNORED")
                    )
                    continue
                await self._open(conn, ev, now)
                await conn.execute(
                    update(market_events)
                    .where(market_events.c.id == ev.id)
                    .values(status="PROCESSED")
                )
                opened += 1
        return opened

    async def _open(self, conn: AsyncConnection, ev: Any, now: datetime) -> uuid.UUID:
        key = f"mission:{ev.id}"
        existing = (
            await conn.execute(select(missions.c.id).where(missions.c.idempotency_key == key))
        ).first()
        if existing is not None:
            return uuid.UUID(str(existing.id))
        entry = ev.kind == "SIGNAL" and (ev.detail or {}).get("action") == "ENTER_LONG"
        mid = uuid7()
        expires = now + MISSION_TTL
        await conn.execute(
            missions.insert().values(
                id=mid,
                kind="entry_signal" if entry else "event_review",
                trigger_event_id=ev.id,
                instrument_id=ev.instrument_id,
                status="OPEN",
                idempotency_key=key,
                ai_call_budget=2 if entry else 0,
                expires_at=expires,
                created_at=now,
                updated_at=now,
            )
        )
        version_id = await ensure_strategy(conn, self.strategy, now)
        oid = await opp.create(
            conn,
            mission_id=mid,
            instrument_id=ev.instrument_id,
            now=now,
            strategy_version_id=version_id,
        )
        acc = {"evidence": "required", "timestamps": "required"}
        t_data = await engine.create_task(
            conn,
            mission_id=mid,
            role_key="source_data_analyst",
            kind="data_check",
            title="Verify data freshness and agreement",
            now=now,
            expires_at=expires,
            idempotency_key=f"{key}:data",
            acceptance=acc,
            input={"opportunity_id": str(oid)},
        )
        t_quant = await engine.create_task(
            conn,
            mission_id=mid,
            role_key="quant_analyst",
            kind="quant",
            title="Indicators, regime, signal, backtest, sizing",
            now=now,
            expires_at=expires,
            idempotency_key=f"{key}:quant",
            acceptance=acc,
            depends_on=[t_data],
        )
        last = t_quant
        if entry:
            t_narr = await engine.create_task(
                conn,
                mission_id=mid,
                role_key="market_researcher",
                kind="narrative",
                title="Thesis, counterarguments, red team",
                now=now,
                expires_at=expires,
                idempotency_key=f"{key}:narrative",
                acceptance=acc,
                depends_on=[t_quant],
            )
            last = await engine.create_task(
                conn,
                mission_id=mid,
                role_key="risk_reviewer",
                kind="proposal",
                title="Typed candidate and deterministic pre-checks",
                now=now,
                expires_at=expires,
                idempotency_key=f"{key}:proposal",
                acceptance=acc,
                depends_on=[t_narr],
            )
        await engine.create_task(
            conn,
            mission_id=mid,
            role_key="orchestrator",
            kind="report",
            title="Change-first report",
            now=now,
            expires_at=expires,
            idempotency_key=f"{key}:report",
            acceptance={"sections": "all"},
            depends_on=[last],
        )
        await record(
            conn,
            action="mission.opened",
            actor=Actor.worker("orchestrator"),
            occurred_at=now,
            entity_type="mission",
            entity_id=mid,
            payload={"event": ev.kind, "entry_signal": entry},
        )
        return mid

    # ---- stepping -----------------------------------------------------------------------

    async def step(self, worker_id: str = "worker") -> int:
        now = self.d.clock.now()
        done = 0
        async with self.d.db.transaction() as conn:
            batch = await engine.runnable(conn, now)
            started = [
                (t, await engine.start(conn, t, worker_id=worker_id, now=now)) for t in batch
            ]
            for t, _ in started:
                await conn.execute(
                    update(missions)
                    .where(missions.c.id == t.mission_id, missions.c.status == "OPEN")
                    .values(status="RUNNING", updated_at=now)
                )
        for t, attempt_id in started:
            await self._run_task(t, attempt_id, worker_id)
            done += 1
        return done

    async def _run_task(self, t: Any, attempt_id: uuid.UUID, worker_id: str) -> None:
        handler = {
            "data_check": self._data_check,
            "quant": self._quant,
            "narrative": self._narrative,
            "proposal": self._proposal,
            "report": self._report,
        }[t.kind]
        toolbox = Toolbox(t.role_key, self._tools())
        try:
            outcome = await handler(t, toolbox)
        except Exception as exc:  # contain failures to the task; never crash the worker
            log.warning("task_failed", kind=t.kind, error=type(exc).__name__, detail=str(exc)[:200])
            now = self.d.clock.now()
            async with self.d.db.transaction() as conn:
                await engine.finish_attempt(
                    conn, attempt_id, "FAILED", now, f"{type(exc).__name__}: {exc}"
                )
                await engine.fail(conn, t, f"{type(exc).__name__}: {exc}", now)
            return
        now = self.d.clock.now()
        async with self.d.db.transaction() as conn:
            if outcome.get("_waiting_for_ai"):
                await engine.finish_attempt(conn, attempt_id, "WAITING_FOR_AI", now)
                await engine.wait_for_ai(conn, t, str(outcome["_waiting_for_ai"]), now)
                await conn.execute(
                    update(missions)
                    .where(missions.c.id == t.mission_id)
                    .values(
                        status="WAITING_FOR_AI",
                        status_reason=str(outcome["_waiting_for_ai"])[:300],
                        updated_at=now,
                    )
                )
                return
            if outcome.get("_failed"):
                await engine.finish_attempt(
                    conn, attempt_id, "FAILED", now, str(outcome["_failed"])
                )
                await engine.fail(
                    conn, t, str(outcome["_failed"]), now, retryable=bool(outcome.get("_retryable"))
                )
                return
            await engine.finish_attempt(conn, attempt_id, "DONE", now)
            status = await engine.submit_output(conn, t, outcome, now)
            if status == "IN_REVIEW":
                await engine.review(conn, t.id, self._acceptance(t.kind, outcome), now)
            if t.kind == "quant" and outcome.get("signal") == "ENTER_LONG":
                await self._prioritise_narrative(conn, t.mission_id, outcome, now)
            await conn.execute(
                update(missions)
                .where(missions.c.id == t.mission_id, missions.c.status == "WAITING_FOR_AI")
                .values(status="RUNNING", status_reason=None, updated_at=now)
            )

    @staticmethod
    async def _prioritise_narrative(
        conn: AsyncConnection, mission_id: uuid.UUID, quant: dict[str, Any], now: datetime
    ) -> None:
        """Order waiting AI work by decision value under a finite free quota (AI-20)."""
        priority, reason = narrative_priority(quant)
        result = await conn.execute(
            update(tasks)
            .where(
                tasks.c.mission_id == mission_id,
                tasks.c.kind == "narrative",
                tasks.c.status.in_(["QUEUED", "WAITING_FOR_AI"]),
            )
            .values(priority=priority, updated_at=now)
        )
        if result.rowcount:
            await record(
                conn,
                action="task.prioritised",
                actor=Actor.system("workforce"),
                occurred_at=now,
                entity_type="mission",
                entity_id=mission_id,
                payload={"task": "narrative", "priority": priority, "reason": reason},
            )

    @staticmethod
    def _acceptance(kind: str, out: dict[str, Any]) -> dict[str, bool]:
        """Deterministic verification by the task reviewer (WF-05)."""
        if kind == "data_check":
            return {
                "quote_timestamp_present": bool(out.get("quote", {}).get("source_ts")),
                "label_reported": "label" in out.get("quote", {}),
                "bar_count_reported": "bars" in out,
            }
        if kind == "quant":
            return {
                "signal_reported": "signal" in out,
                "regime_reported": "regime" in out,
                "disclaimer_present": "disclaimer" in str(out.get("backtest", {}))
                or out.get("backtest") is None,
                "costs_disclosed": "assumptions" in str(out.get("backtest", {}))
                or out.get("backtest") is None,
            }
        if kind == "narrative":
            return {
                "has_counterarguments": bool(out.get("counterarguments")),
                "claims_classified": all(
                    c.get("status") in ("VERIFIED", "INFERRED", "CONTRADICTED")
                    for c in out.get("claims", [])
                ),
            }
        if kind == "proposal":
            return {
                "outcome_recorded": out.get("status") in ("PROPOSED", "NON_EXECUTABLE", "NO_TRADE")
            }
        return {"output_present": bool(out)}

    # ---- tools (allowlisted per role) ---------------------------------------------------------

    def _tools(self) -> dict[str, Any]:
        return {
            "market.read": self._noop,
            "research.analyse": self._noop,
            "trading.size": self._noop,
            "evidence.claim": self._noop,
            "ops.read": self._noop,
            "trading.propose": self._propose_tool,
            "ai.research_narrative": self._ai_tool,
        }

    async def _noop(self, *_a: Any, **_k: Any) -> None:
        return None

    async def _ai_tool(self, task: AITask) -> Any:
        if self.d.ai is None:
            return None
        return await self.d.ai.run(task)

    async def _propose_tool(self, candidate: Candidate) -> Any:
        async with self.d.db.transaction() as conn:
            return await create_candidate(
                conn,
                candidate=candidate,
                actor=Actor.worker("risk_reviewer"),
                now=self.d.clock.now(),
            )

    # ---- handlers ---------------------------------------------------------------------------

    async def _mission(self, conn: AsyncConnection, mission_id: uuid.UUID) -> Any:
        return (await conn.execute(select(missions).where(missions.c.id == mission_id))).one()

    async def _output(
        self, conn: AsyncConnection, mission_id: uuid.UUID, kind: str
    ) -> dict[str, Any]:
        row = (
            await conn.execute(
                select(tasks.c.output).where(tasks.c.mission_id == mission_id, tasks.c.kind == kind)
            )
        ).first()
        return dict(row.output or {}) if row is not None else {}

    async def _opportunity_id(self, conn: AsyncConnection, mission_id: uuid.UUID) -> uuid.UUID:
        return uuid.UUID(
            str(
                (
                    await conn.execute(
                        select(opportunities.c.id).where(opportunities.c.mission_id == mission_id)
                    )
                ).scalar_one()
            )
        )

    async def _data_check(self, t: Any, tb: Toolbox) -> dict[str, Any]:
        await tb.call("market.read")
        now = self.d.clock.now()
        async with self.d.db.transaction() as conn:
            m = await self._mission(conn, t.mission_id)
            inst = await md.instrument_by_id(conn, m.instrument_id)
            q = await md.latest_quote(conn, inst.id, now)
            dev = await md.cross_source_deviation(conn, inst.pair_key, now)
            bars = await md.complete_bars(conn, inst.id, "1h", limit=800)
            problems = []
            if q.label is not DataLabel.DELAYED:
                problems.append(f"quote is {q.label.value}")
            if len(bars) < MIN_BARS:
                problems.append(f"only {len(bars)} complete hourly bars (< {MIN_BARS})")
            if dev.label is not DataLabel.DELAYED:
                problems.append(f"cross-venue check {dev.label.value}")
            await add_claim(
                conn,
                mission_id=t.mission_id,
                statement=f"Latest {inst.canonical_key} mid",
                status="VERIFIED",
                created_by="source_data_analyst",
                now=now,
                metric="mid",
                value=q.mid,
                unit=inst.quote,
                as_of=q.source_ts,
                subject=("instrument", inst.canonical_key),
                method="read from ingested quote",
                evidence=[("QUOTE", f"{q.source_key}:{inst.canonical_key}", q.source_ts)],
            )
            if dev.bps is not None:
                await add_claim(
                    conn,
                    mission_id=t.mission_id,
                    statement="Cross-venue mid deviation",
                    status="VERIFIED",
                    created_by="source_data_analyst",
                    now=now,
                    metric="deviation_bps",
                    value=dev.bps,
                    unit="bps",
                    as_of=now,
                    method="computed from fresh venue quotes",
                )
        return {
            "instrument": inst.canonical_key,
            "quote": {
                "bid": q.bid,
                "ask": q.ask,
                "label": q.label.value,
                "source_ts": q.source_ts,
                "age_s": q.age_seconds,
            },
            "deviation": {"bps": dev.bps, "label": dev.label.value},
            "bars": len(bars),
            "last_bar": bars[-1].open_time if bars else None,
            "problems": problems,
        }

    async def _quant(self, t: Any, tb: Toolbox) -> dict[str, Any]:
        await tb.call("research.analyse")
        now = self.d.clock.now()
        async with self.d.db.transaction() as conn:
            m = await self._mission(conn, t.mission_id)
            inst = await md.instrument_by_id(conn, m.instrument_id)
            bars = await md.complete_bars(conn, inst.id, "1h", limit=2000)
            if len(bars) < self.strategy.warmup() + 2:
                return {
                    "signal": "HOLD",
                    "regime": "INSUFFICIENT_DATA",
                    "backtest": None,
                    "problems": [f"only {len(bars)} bars"],
                    "sizing": None,
                }
            closes = [float(b.close) for b in bars]
            sig = self.strategy.signal(bars)
            regime = classify(closes)
            rsi = ind.rsi(closes, 14)[-1]
            atr_v = ind.atr(
                [float(b.high) for b in bars], [float(b.low) for b in bars], closes, 14
            )[-1]
            walk = (168, 72) if len(bars) >= 240 else None
            bt = await run_and_store(
                conn, strategy=self.strategy, instrument_id=inst.id, now=now, walk=walk
            )
            for metric, value in (("rsi14", rsi), ("atr14", atr_v), ("last_close", closes[-1])):
                if value is not None:
                    await add_claim(
                        conn,
                        mission_id=t.mission_id,
                        statement=f"{metric} of {inst.canonical_key}",
                        status="VERIFIED",
                        created_by="quant_analyst",
                        now=now,
                        metric=metric,
                        value=Decimal(str(round(value, 8))),
                        as_of=bars[-1].open_time,
                        method="deterministic indicator on completed bars",
                        evidence=[
                            (
                                "BAR",
                                f"{inst.canonical_key}:1h:{bars[-1].open_time.isoformat()}",
                                bars[-1].open_time,
                            )
                        ],
                    )
            sizing = (
                await self._sizing(conn, inst, sig.stop, now)
                if sig.action == "ENTER_LONG"
                else None
            )
            oid = await self._opportunity_id(conn, t.mission_id)
            await opp.advance(
                conn,
                oid,
                "RESEARCH",
                "data checked and quant analysis complete",
                now,
                direction="LONG" if sig.action == "ENTER_LONG" else None,
            )
        return {
            "instrument": inst.canonical_key,
            "signal": sig.action,
            "signal_reason": sig.reason,
            "stop": sig.stop,
            "take_profit": sig.take_profit,
            "regime": regime.label,
            "indicators": {"rsi14": rsi, "atr14": atr_v, "last_close": closes[-1], **sig.detail},
            "backtest": bt.get("backtest"),
            "walk_forward": bt.get("walk_forward"),
            "backtest_run_id": bt.get("run_id"),
            "sizing": sizing,
        }

    async def _sizing(
        self, conn: AsyncConnection, inst: md.InstrumentView, stop: float | None, now: datetime
    ) -> dict[str, Any] | None:
        acct_id = await paper_account_for_venue(conn, inst.venue_key)
        q = await md.latest_quote(conn, inst.id, now)
        if (
            acct_id is None
            or stop is None
            or q.ask is None
            or inst.lot_size is None
            or inst.tick_size is None
        ):
            return None
        acct = await get_account(conn, account_id=acct_id)
        state = await account_state(conn, acct, now)
        _, policy = await active_policy(conn)
        if state.equity is None or state.cash_available is None:
            return None
        entry = q.ask
        stop_d = Decimal(str(round(stop, 2)))
        result = size_long(
            equity=state.equity,
            entry=entry,
            stop=stop_d,
            risk_per_trade_pct=policy.risk_per_trade_pct,
            uncertainty_haircut_pct=policy.uncertainty_haircut_pct,
            lot_size=inst.lot_size,
            min_qty=inst.min_qty,
            min_notional=inst.min_notional,
            max_order_notional=policy.max_order_notional,
            position_headroom_notional=policy.max_position_notional,
            cash_available=state.cash_available,
            fee_bps=policy.fee_bps_assumption,
        )
        return {
            "account_alias": acct.alias,
            "quantity": result.quantity,
            "entry": entry,
            "stop": stop_d,
            "binding_constraint": result.binding_constraint,
            "notes": list(result.notes),
        }

    async def _narrative(self, t: Any, tb: Toolbox) -> dict[str, Any]:
        async with self.d.db.connect() as conn:
            m = await self._mission(conn, t.mission_id)
            quant = await self._output(conn, t.mission_id, "quant")
            data = await self._output(conn, t.mission_id, "data_check")
        if m.ai_calls_used >= m.ai_call_budget:
            return {"_failed": "mission AI budget exhausted", "_retryable": False}
        items = [
            UntrustedItem(
                "quant",
                "deterministic analysis",
                str(
                    {
                        k: quant.get(k)
                        for k in (
                            "instrument",
                            "signal",
                            "signal_reason",
                            "regime",
                            "indicators",
                            "stop",
                            "take_profit",
                        )
                    }
                ),
            ),
            UntrustedItem(
                "backtest",
                "historical simulation",
                str(
                    (quant.get("backtest") or {})
                    and {
                        k: quant["backtest"].get(k)
                        for k in ("trades", "total_return", "max_drawdown", "notes")
                    }
                ),
            ),
            UntrustedItem(
                "data",
                "data check",
                str(
                    {
                        "quote": data.get("quote"),
                        "deviation": data.get("deviation"),
                        "problems": data.get("problems"),
                    }
                ),
            ),
        ]
        task = AITask(
            task_type="research_narrative",
            role_key="market_researcher",
            instructions=(
                "Write a cautious thesis for the deterministic signal, the strongest "
                "counterarguments, a red-team view, scenarios and invalidation conditions. "
                "Cite only numbers present in the data; use metric names rsi14, atr14 or "
                "last_close for numeric claims."
            ),
            data=items,
            output_model=Narrative,
            classification=DataClass.PUBLIC,
            max_tokens=900,
        )
        result = await tb.call("ai.research_narrative", task)
        async with self.d.db.transaction() as conn:
            await conn.execute(
                update(missions)
                .where(missions.c.id == t.mission_id)
                .values(ai_calls_used=missions.c.ai_calls_used + 1)
            )
        if result is None or result.status == "WAITING_FOR_AI":
            reasons = (
                "; ".join(result.reasons[:3]) if result is not None else "AI gateway not configured"
            )
            return {"_waiting_for_ai": f"no eligible free AI route: {reasons}"}
        if result.status != "OK" and result.status != "CACHED":
            return {
                "_failed": f"AI {result.status}: {'; '.join(result.reasons[:3])}",
                "_retryable": True,
            }
        narrative = result.output
        assert isinstance(narrative, Narrative)
        now = self.d.clock.now()
        indicators = quant.get("indicators", {})
        classified = []
        async with self.d.db.transaction() as conn:
            for c in narrative.claims:
                status = "INFERRED"
                if (
                    c.metric in indicators
                    and c.value is not None
                    and indicators[c.metric] is not None
                ):
                    ok = verify_numeric(Decimal(str(c.value)), Decimal(str(indicators[c.metric])))
                    status = "VERIFIED" if ok else "CONTRADICTED"
                await add_claim(
                    conn,
                    mission_id=t.mission_id,
                    statement=c.statement,
                    status=status,
                    created_by=f"market_researcher:{result.provider_key}/{result.model_id}",
                    now=now,
                    metric=c.metric,
                    value=Decimal(str(c.value)) if c.value is not None else None,
                    method="AI statement re-checked against computed indicators"
                    if status != "INFERRED"
                    else "AI statement (not independently verified)",
                )
                classified.append({"statement": c.statement, "status": status})
            oid = await self._opportunity_id(conn, t.mission_id)
            await opp.advance(
                conn,
                oid,
                "CHALLENGED",
                "thesis and counterarguments written",
                now,
                thesis=narrative.thesis[:2000],
            )
        return {
            **narrative.model_dump(),
            "claims": classified,
            "provider": result.provider_key,
            "model": result.model_id,
            "label": "AI-generated, schema-validated; numbers re-checked by code",
        }

    async def _proposal(self, t: Any, tb: Toolbox) -> dict[str, Any]:
        now = self.d.clock.now()
        async with self.d.db.connect() as conn:
            quant = await self._output(conn, t.mission_id, "quant")
            narr = await self._output(conn, t.mission_id, "narrative")
            data = await self._output(conn, t.mission_id, "data_check")
            oid = await self._opportunity_id(conn, t.mission_id)
            m = await self._mission(conn, t.mission_id)
            inst = await md.instrument_by_id(conn, m.instrument_id)
        sizing = quant.get("sizing")
        if (
            quant.get("signal") != "ENTER_LONG"
            or not sizing
            or Decimal(str(sizing["quantity"])) <= ZERO
        ):
            reason = (
                "no entry signal"
                if quant.get("signal") != "ENTER_LONG"
                else "position size rounds to zero"
            )
            async with self.d.db.transaction() as conn:
                await opp.advance(conn, oid, "NO_TRADE", reason, now)
            return {"status": "NO_TRADE", "reason": reason}
        tick = inst.tick_size or Decimal("0.01")
        entry = (Decimal(str(sizing["entry"])) / tick).to_integral_value(ROUND_DOWN) * tick
        band_low = (entry * Decimal("0.995") / tick).to_integral_value(ROUND_DOWN) * tick
        band_high = (entry * Decimal("1.005") / tick).to_integral_value(ROUND_DOWN) * tick
        stop = (Decimal(str(quant["stop"])) / tick).to_integral_value(ROUND_DOWN) * tick
        stop_limit = (stop * Decimal("0.998") / tick).to_integral_value(ROUND_DOWN) * tick
        tp = (Decimal(str(quant["take_profit"])) / tick).to_integral_value(ROUND_DOWN) * tick
        candidate = Candidate.model_validate(
            {
                "account_alias": sizing["account_alias"],
                "instrument_key": inst.canonical_key,
                "side": "BUY",
                "effect": "OPEN",
                "quantity": Decimal(str(sizing["quantity"])),
                "limit_price": entry,
                "price_band_low": band_low,
                "price_band_high": band_high,
                "max_slippage_bps": Decimal(10),
                "time_in_force": "GTC",
                "horizon": "hours to days",
                "thesis": narr["thesis"],
                "plain_summary": narr["plain_summary"],
                "evidence": [
                    {
                        "kind": "QUOTE",
                        "ref": f"mission:{t.mission_id}:data_check",
                        "summary": f"quote {data['quote']['label']} bid {data['quote']['bid']} ask "
                        f"{data['quote']['ask']}",
                        "source_ts": data["quote"]["source_ts"],
                    },
                    {
                        "kind": "INDICATOR",
                        "ref": f"mission:{t.mission_id}:quant",
                        "summary": f"{quant['signal_reason']}; regime {quant['regime']}",
                    },
                    {
                        "kind": "BACKTEST",
                        "ref": str(quant.get("backtest_run_id")),
                        "summary": "historical simulation, assumed costs; not evidence of profit",
                    },
                ],
                "counterarguments": narr["counterarguments"],
                "red_team": narr["red_team"],
                "scenarios": narr["scenarios"],
                "invalidation": narr["invalidation"],
                "entry_conditions": f"Limit buy at or below {entry}",
                "opportunity_id": oid,
                "management_plan": {
                    "stop_loss": {"stop_price": stop, "limit_price": stop_limit},
                    "take_profit_price": tp,
                    "cancel_unfilled_after_seconds": 3600,
                },
            }
        )
        async with self.d.db.transaction() as conn:
            await opp.advance(
                conn, oid, "QUALIFIED", "typed candidate built from verified analysis", now
            )
        result = await tb.call("trading.propose", candidate)
        async with self.d.db.transaction() as conn:
            if result.status == "PROPOSED":
                await opp.advance(
                    conn,
                    oid,
                    "APPROVAL_REQUIRED",
                    "deterministic pre-checks passed",
                    now,
                    intent_id=result.intent_id,
                )
            else:
                await opp.advance(
                    conn,
                    oid,
                    "NON_EXECUTABLE",
                    "; ".join(result.problems)[:400],
                    now,
                    intent_id=result.intent_id,
                )
        return {
            "status": result.status,
            "intent_id": str(result.intent_id),
            "trade_code": result.trade_code,
            "problems": list(result.problems),
        }

    async def _report(self, t: Any, tb: Toolbox) -> dict[str, Any]:
        now = self.d.clock.now()
        async with self.d.db.transaction() as conn:
            m = await self._mission(conn, t.mission_id)
            ev = (
                await conn.execute(
                    select(market_events).where(market_events.c.id == m.trigger_event_id)
                )
            ).first()
            data = await self._output(conn, t.mission_id, "data_check")
            quant = await self._output(conn, t.mission_id, "quant")
            narr = await self._output(conn, t.mission_id, "narrative")
            prop = await self._output(conn, t.mission_id, "proposal")
            cl = await mission_claims(conn, t.mission_id)
            unknown = list(data.get("problems", [])) + list(quant.get("problems", []) or [])
            if m.kind == "entry_signal" and not narr:
                unknown.append("no AI narrative (free AI unavailable or not configured)")
            report = {
                "what_changed": {
                    "event": ev.kind if ev else None,
                    "detail": ev.detail if ev else None,
                    "bar_time": ev.bar_time if ev else None,
                },
                "what_matters": {
                    "signal": quant.get("signal"),
                    "regime": quant.get("regime"),
                    "proposal": prop.get("status", "none"),
                    "trade_code": prop.get("trade_code"),
                },
                "verified": [
                    c["statement"] + f" = {c['value']}" for c in cl if c["status"] == "VERIFIED"
                ],
                "inferred": [c["statement"] for c in cl if c["status"] == "INFERRED"],
                "contradicts": [c["statement"] for c in cl if c["status"] == "CONTRADICTED"]
                + list(narr.get("counterarguments", [])),
                "unknown": unknown,
                "timestamps": {
                    "quote": data.get("quote", {}).get("source_ts"),
                    "last_bar": data.get("last_bar"),
                    "report": now,
                },
                "freshness": {
                    "quote": data.get("quote", {}).get("label"),
                    "cross_venue": data.get("deviation", {}).get("label"),
                },
                "why_it_matters": (
                    narr.get("plain_summary")
                    or f"{quant.get('signal')} on {quant.get('instrument')}; deterministic only."
                ),
                "backtest": quant.get("backtest"),
                "disclaimer": "Research output, not authorisation. Trades need explicit approval.",
            }
            aid = await engine.add_artifact(
                conn, mission_id=t.mission_id, task_id=t.id, kind="report", content=report, now=now
            )
            await conn.execute(
                update(missions)
                .where(missions.c.id == t.mission_id)
                .values(
                    status="ARCHIVED",
                    completed_at=now,
                    updated_at=now,
                    status_reason="report archived",
                )
            )
            await record(
                conn,
                action="mission.completed",
                actor=Actor.worker("orchestrator"),
                occurred_at=now,
                entity_type="mission",
                entity_id=t.mission_id,
                payload={"report": aid},
            )
        return {"report_artifact": str(aid), **{k: report[k] for k in ("what_matters", "unknown")}}

    # ---- housekeeping -----------------------------------------------------------------------

    async def expire(self) -> int:
        """Missions past expiry end without a proposal (no AI → no AI-researched proposal)."""
        now = self.d.clock.now()
        n = 0
        async with self.d.db.transaction() as conn:
            rows = list(
                await conn.execute(
                    select(missions).where(
                        missions.c.status.in_(["OPEN", "RUNNING", "WAITING_FOR_AI"]),
                        missions.c.expires_at <= now,
                    )
                )
            )
            await engine.expire_sweep(conn, now)
            for m in rows:
                await conn.execute(
                    update(missions)
                    .where(missions.c.id == m.id)
                    .values(
                        status="EXPIRED",
                        updated_at=now,
                        completed_at=now,
                        status_reason=m.status_reason or "mission expired before completion",
                    )
                )
                oid = await self._opportunity_id(conn, m.id)
                row = (
                    await conn.execute(
                        select(opportunities.c.status).where(opportunities.c.id == oid)
                    )
                ).scalar_one()
                if row in ("IDEA", "RESEARCH", "CHALLENGED", "QUALIFIED"):
                    await opp.advance(
                        conn, oid, "NO_TRADE", "mission expired (e.g. free AI unavailable)", now
                    )
                n += 1
        return n


async def mission_report(conn: AsyncConnection, mission_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        await conn.execute(
            select(artifacts.c.content).where(
                artifacts.c.mission_id == mission_id, artifacts.c.kind == "report"
            )
        )
    ).first()
    return None if row is None else dict(row.content)
