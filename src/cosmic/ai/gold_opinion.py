"""The AI's second opinion on HFM's gold (S68, ADR-037 addendum; the owner, 2026-11-05: "Use AI as
an analytical second opinion for M1, H1 and D1 … The risk engine must remain in control").

Every ``EVERY`` minutes, and only through the free-AI gateway (FREE_CLOUD_AI_ONLY: $0, fail
closed), a model reads what the rules read: HFM's own candles from 1 minute to daily, as numbers
(trend, momentum, volatility, support and resistance), HFM's price and spread, the session. It
answers in a fixed form: the direction, BUY, SELL or WAIT, why, the market conditions, the
expected risk and whether now suits a trade. The answer is stored (``ai_gold_opinions``).

It decides nothing: the gold engine (which never imports this module) reads the latest stored
opinion and lets it stop a trade (WAIT, or the other direction), never start one, size one or
loosen a limit. Without a fresh opinion (no AI configured, or none for 15 minutes) the rules
trade alone, as the owner chose."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select

from cosmic.ai.gateway import AIGateway, AIResult, AITask
from cosmic.ai.safety import DataClass, UntrustedItem
from cosmic.core.ids import uuid7
from cosmic.core.logs import get_logger
from cosmic.db.engine import Database
from cosmic.db.schema_gold import ai_gold_opinions
from cosmic.gold import analysis, sim, timing

log = get_logger(__name__)
EVERY = timedelta(minutes=5)  # within the free tiers' limits


class GoldOpinion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Literal["UP", "DOWN", "SIDEWAYS"]
    recommendation: Literal["BUY", "SELL", "WAIT"]
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    why: str = Field(min_length=10, max_length=500)
    conditions: str = Field(min_length=5, max_length=400)
    expected_risk: str = Field(min_length=5, max_length=300)
    timing: str = Field(min_length=5, max_length=300)


INSTRUCTIONS = (
    "You are a cautious second opinion on HFM's gold (XAUUSD) for an automated demo trader. The "
    "data are HFM's own candles read on 1-minute, 5-minute, 15-minute, hourly and daily "
    "timeframes (trend, RSI, ATR as volatility, support and resistance), HFM's price and spread, "
    "and the trading session. Say the market's direction; recommend BUY, SELL or WAIT; explain "
    "why the opportunity exists or not, the market conditions, the expected risk (in price terms "
    "and what could go wrong) and whether now is a suitable time. Use only numbers present in "
    "the data; never invent news or levels. When the timeframes disagree, or volatility is too "
    "low or too high, recommend WAIT. Your answer can only stop a trade, never start one."
)


def _frames(reading: analysis.Reading) -> str:
    rows = []
    for tf, f in reading.frames.items():
        rows.append(
            f"{tf}: candles {f.candles}, trend {f.trend}, close {f.close}, "
            f"RSI {f.rsi and round(f.rsi, 1)}, "
            f"ATR {f.atr and round(f.atr, 2)} ({f.atr_pct and round(f.atr_pct * 100, 3)} %), "
            f"support {f.support}, resistance {f.resistance}"
        )
    return "\n".join(rows)


async def opine(db: Database, gateway: AIGateway, now: datetime) -> AIResult | None:
    """One opinion, if the last is older than ``EVERY`` and HFM's market is open; None
    otherwise. The result is stored when the model answered in the form."""
    async with db.connect() as conn:
        last = await conn.scalar(
            select(ai_gold_opinions.c.at).order_by(desc(ai_gold_opinions.c.at)).limit(1)
        )
        if last is not None and now - last < EVERY:
            return None
        price, feed = await sim.feed(conn, now)
        if price is None:
            return None  # a closed market or a silent bridge: nothing new to read
        reading = await sim.reading_now(conn, now)
    when = timing.timing(now, reading, price.spread, feed == sim.FEED_OK)
    data = [
        UntrustedItem("frames", "HFM's candles, read by the rules", _frames(reading)),
        UntrustedItem(
            "price",
            "HFM's price",
            f"bid {price.bid}, ask {price.ask}, spread {price.spread}; the rules say "
            f"{reading.decision}: {'; '.join(reading.reasons)}",
        ),
        UntrustedItem(
            "session",
            "the market timing",
            f"sessions open: {', '.join(when.open_sessions) or 'none'}; suitability "
            f"{when.suitability}: {when.why}",
        ),
    ]
    task = AITask(
        task_type="market_opinion",
        role_key="gold_second_opinion",
        instructions=INSTRUCTIONS,
        data=data,
        output_model=GoldOpinion,
        classification=DataClass.PUBLIC,  # public market data only; nothing of the account
        max_tokens=700,
        cache_ttl_seconds=240,
    )
    result = await gateway.run(task)
    if result.status in ("OK", "CACHED") and isinstance(result.output, GoldOpinion):
        o = result.output
        async with db.transaction() as conn:
            await conn.execute(
                ai_gold_opinions.insert().values(
                    id=uuid7(),
                    at=now,
                    bar=reading.bar,
                    provider_key=result.provider_key or "unknown",
                    model_id=result.model_id or "unknown",
                    reading=reading.decision,
                    direction=o.direction,
                    recommendation=o.recommendation,
                    confidence=o.confidence,
                    why=o.why,
                    conditions=o.conditions,
                    expected_risk=o.expected_risk,
                    timing=o.timing,
                )
            )
    else:
        log.info("gold_opinion_unavailable", status=result.status, reasons=result.reasons[:2])
    return result


def summary(row: Any) -> dict[str, Any]:
    return {k: getattr(row, k) for k in ("at", "recommendation", "direction", "confidence", "why")}
