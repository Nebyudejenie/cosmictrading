"""End-to-end trading environment on the real test database (PAPER only)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_market import fetch_runs, instruments, quotes, sources, venue_status, venues
from cosmic.identity.service import Principal
from cosmic.market.jobs import build_ingest_service
from cosmic.sources.ingest import ensure_registry
from cosmic.trading import authority
from cosmic.trading.approvals import ApprovalRecord, decide, issue_challenge
from cosmic.trading.brokers.paper import PaperConfig
from cosmic.trading.modes import Mode
from cosmic.trading.paper_accounts import open_paper_account
from cosmic.trading.policy import ensure_default_policy
from cosmic.trading.proposals import Candidate, ProposalResult, create_candidate
from cosmic.trading.services import TradingServices
from cosmic.worker.context import WorkerContext
from tests.helpers import TestUser, make_user, principal_for
from tests.market_mock import MarketMock

D = Decimal
CAPTURE = datetime(2026, 10, 10, 21, 6, 0, tzinfo=UTC)
BTC = "SPOT:BTC/USD@COINBASE"


def candidate(**kw: Any) -> Candidate:
    base: dict[str, Any] = {
        "account_alias": "paper-main",
        "instrument_key": BTC,
        "side": "BUY",
        "effect": "OPEN",
        "quantity": D("0.004"),
        "limit_price": D("83000.00"),
        "price_band_low": D("82600.00"),
        "price_band_high": D("83400.00"),
        "max_slippage_bps": D("10"),
        "time_in_force": "GTC",
        "horizon": "days",
        "thesis": "Trend continuation after a higher low on the hourly chart.",
        "plain_summary": "Buy a small amount of BTC because the short-term trend is up.",
        "evidence": [
            {
                "kind": "QUOTE",
                "ref": "coinbase BTC-USD ticker",
                "summary": "fresh quote",
                "source_ts": CAPTURE,
            }
        ],
        "counterarguments": ["Momentum signals often fail in ranging markets."],
        "red_team": "The move may already be priced in; volume is not confirming.",
        "scenarios": [{"name": "base", "description": "Drift higher over several days."}],
        "invalidation": ["Hourly close below 82,000."],
        "entry_conditions": "Limit buy at or below 83,000.",
        "management_plan": {
            "stop_loss": {"stop_price": D("81000.00"), "limit_price": D("80900.00")}
        },
    }
    base.update(kw)
    return Candidate.model_validate(base)


@dataclass
class Env:
    settings: Settings
    db: Database
    clock: FrozenClock
    svc: TradingServices
    owner: TestUser
    account_id: uuid.UUID
    extra: dict[str, Any] = field(default_factory=dict)

    def principal(self, *, mfa_fresh: bool = True) -> Principal:
        return principal_for(self.owner, self.clock, mfa_fresh=mfa_fresh)

    async def propose(self, **kw: Any) -> ProposalResult:
        async with self.db.transaction() as conn:
            return await create_candidate(
                conn,
                candidate=candidate(**kw),
                actor=Actor.user(self.owner.user_id),
                now=self.clock.now(),
            )

    async def approve(self, intent_id: uuid.UUID, decision: str = "APPROVE") -> ApprovalRecord:
        p = self.principal()
        async with self.db.transaction() as conn:
            ch = await issue_challenge(
                conn, principal=p, intent_id=intent_id, channel="PORTAL", now=self.clock.now()
            )
        async with self.db.transaction() as conn:
            return await decide(
                conn,
                principal=p,
                token=ch.token,
                packet_hash=ch.packet_hash,
                decision=decision,
                now=self.clock.now(),
                settings=self.settings,
            )  # type: ignore[arg-type]

    async def set_market(self, bid: str, ask: str, *, pair: str = "BTC/USD") -> None:
        """New fresh quotes for both venues of a pair (deterministic price moves)."""
        now = self.clock.now()
        async with self.db.transaction() as conn:
            rows = list(
                await conn.execute(
                    select(instruments.c.id, instruments.c.canonical_key).where(
                        instruments.c.pair_key == pair
                    )
                )
            )
            for r in rows:
                src_key = (
                    "coinbase_exchange_public"
                    if r.canonical_key.endswith("COINBASE")
                    else "bitstamp_public"
                )
                src_id = (
                    await conn.execute(select(sources.c.id).where(sources.c.key == src_key))
                ).scalar_one()
                run_id = uuid7()
                await conn.execute(
                    fetch_runs.insert().values(
                        id=run_id,
                        source_id=src_id,
                        kind="test_quote",
                        started_at=now,
                        finished_at=now,
                        status="SUCCESS",
                    )
                )
                await conn.execute(
                    quotes.insert().values(
                        id=uuid7(),
                        instrument_id=r.id,
                        source_id=src_id,
                        bid=D(bid),
                        ask=D(ask),
                        last=D(bid),
                        source_ts=now,
                        ingested_at=now,
                        fetch_run_id=run_id,
                    )
                )
            venue_id = (
                await conn.execute(select(venues.c.id).where(venues.c.key == "COINBASE"))
            ).scalar_one()
            st = (
                await conn.execute(select(sources.c.id).where(sources.c.key == "coinbase_status"))
            ).scalar_one()
            await conn.execute(
                venue_status.insert().values(
                    id=uuid7(),
                    venue_id=venue_id,
                    source_id=st,
                    status="OPERATIONAL",
                    indicator="none",
                    description="test",
                    source_ts=now,
                    ingested_at=now,
                )
            )

    async def cycle(self, ticks: int = 1) -> None:
        """Venue matching + execution authority cycle (as the worker would run it)."""
        for _ in range(ticks):
            await self.svc.paper.tick()
            await self.svc.execution.run_cycle()

    async def count(self, table: Any, *where: Any) -> int:
        async with self.db.connect() as conn:
            return int(
                await conn.scalar(select(func.count()).select_from(table).where(*where)) or 0
            )


async def build_env(
    settings: Settings,
    db: Database,
    *,
    paper_config: PaperConfig | None = None,
    mode: Mode = Mode.PAPER_TRADING,
    capital: str = "10000",
) -> Env:
    clock = FrozenClock(CAPTURE)
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="test")
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    ingest = build_ingest_service(ctx, transport=MarketMock().transport())
    for key in ("coinbase_exchange_public", "bitstamp_public"):
        await ingest.sync_instruments(key)
        await ingest.ingest_quotes(key)
    await ingest.ingest_venue_status("coinbase_status")
    owner = await make_user(db, clock)
    svc = TradingServices.build(db, clock, settings, paper_config or PaperConfig())
    async with db.transaction() as conn:
        if mode is not Mode.RESEARCH_ONLY:
            await authority.change_mode(
                conn,
                principal=principal_for(owner, clock),
                target=mode,
                reason="enable paper trading for tests",
                now=clock.now(),
                settings=settings,
            )
        account_id = await open_paper_account(
            conn,
            broker=svc.paper,
            alias="paper-main",
            initial_capital=D(capital),
            now=clock.now(),
            actor=Actor.user(owner.user_id),
        )
    result = await svc.reconciler.reconcile(account_id, trigger="account_opened")
    assert result.passed, result.mismatches
    return Env(settings, db, clock, svc, owner, account_id)
