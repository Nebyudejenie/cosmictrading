"""Strategies are deterministic functions of completed bars only (no look-ahead).

``trend_sma_atr`` exists to exercise the pipeline transparently — it is NOT a claim of edge.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from cosmic.core.jsonutil import canonical_hash
from cosmic.market.data import Bar
from cosmic.market.indicators import atr, sma
from cosmic.market.regime import classify


@dataclass(frozen=True)
class Signal:
    action: str  # ENTER_LONG | EXIT | HOLD
    stop: float | None = None
    take_profit: float | None = None
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


class Strategy(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def params(self) -> dict[str, Any]: ...

    def warmup(self) -> int: ...

    def signal(self, history: Sequence[Bar]) -> Signal: ...


@dataclass(frozen=True)
class TrendSmaAtr:
    key: str = "trend_sma_atr"
    params: dict[str, Any] = field(
        default_factory=lambda: {
            "fast": 20,
            "slow": 50,
            "atr_n": 14,
            "stop_k": 2.0,
            "tp_m": 3.0,
            "skip_high_vol": True,
        }
    )

    def warmup(self) -> int:
        return max(int(self.params["slow"]) + 1, int(self.params["atr_n"]) + 1)

    def signal(self, history: Sequence[Bar]) -> Signal:
        p = self.params
        if len(history) < self.warmup():
            return Signal("HOLD", reason="insufficient history")
        closes = [float(b.close) for b in history]
        fast, slow = sma(closes, int(p["fast"])), sma(closes, int(p["slow"]))
        f_now, s_now, f_prev, s_prev = fast[-1], slow[-1], fast[-2], slow[-2]
        if None in (f_now, s_now, f_prev, s_prev):
            return Signal("HOLD", reason="insufficient history")
        assert f_now is not None and s_now is not None and f_prev is not None and s_prev is not None
        a = atr(
            [float(b.high) for b in history],
            [float(b.low) for b in history],
            closes,
            int(p["atr_n"]),
        )[-1]
        detail = {"sma_fast": f_now, "sma_slow": s_now, "atr": a}
        if f_prev <= s_prev and f_now > s_now:
            if (
                p.get("skip_high_vol")
                and len(closes) >= 200
                and classify(closes).volatility == "HIGH_VOL"
            ):
                return Signal(
                    "HOLD", reason="cross up in HIGH_VOL regime (filtered)", detail=detail
                )
            if a is None:
                return Signal("HOLD", reason="ATR unavailable", detail=detail)
            last = closes[-1]
            return Signal(
                "ENTER_LONG",
                stop=last - float(p["stop_k"]) * a,
                take_profit=last + float(p["tp_m"]) * a,
                reason="fast SMA crossed above slow SMA",
                detail=detail,
            )
        if f_prev >= s_prev and f_now < s_now:
            return Signal("EXIT", reason="fast SMA crossed below slow SMA", detail=detail)
        return Signal("HOLD", reason="no crossover", detail=detail)


def version_hash(strategy: Strategy) -> str:
    return canonical_hash(
        {
            "key": strategy.key,
            "params": {k: str(v) for k, v in strategy.params.items()},
            "code_ref": f"cosmic.research.strategy:{type(strategy).__name__}",
        }
    )
