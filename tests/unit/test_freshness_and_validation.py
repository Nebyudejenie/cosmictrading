from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmic.core.labels import DataLabel
from cosmic.sources.contract import BarRecord, QuoteRecord
from cosmic.sources.freshness import classify
from cosmic.sources.ingest import is_complete, validate_bar, validate_quote

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
D = Decimal


def test_labels() -> None:
    assert classify(NOW - timedelta(seconds=30), NOW, 120) is DataLabel.DELAYED
    assert classify(NOW - timedelta(seconds=121), NOW, 120) is DataLabel.STALE
    assert classify(None, NOW, 120) is DataLabel.UNKNOWN
    assert classify(NOW + timedelta(minutes=10), NOW, 120) is DataLabel.UNKNOWN
    assert classify(NOW, NOW, 120, enabled=False) is DataLabel.UNAVAILABLE
    assert classify(NOW, NOW, 120, simulated=True) is DataLabel.SIMULATED
    # Polling is never LIVE; only a licensed stream class could be.
    assert classify(NOW, NOW, 120, latency_class="DELAYED_POLLING") is DataLabel.DELAYED
    assert classify(NOW, NOW, 120, latency_class="LIVE_STREAM") is DataLabel.LIVE


def bar(**kw: object) -> BarRecord:
    base = {
        "source_symbol": "BTC-USD",
        "timeframe": "1h",
        "open_time": NOW - timedelta(hours=2),
        "open": D("100"),
        "high": D("110"),
        "low": D("95"),
        "close": D("105"),
        "volume": D("3"),
    }
    base.update(kw)
    return BarRecord(**base)  # type: ignore[arg-type]


def test_bar_validation() -> None:
    assert validate_bar(bar(), NOW) == []
    checks = {c for c, _ in validate_bar(bar(high=D("101")), NOW)}
    assert "ohlc_consistency" in checks
    assert {c for c, _ in validate_bar(bar(low=D("0")), NOW)} >= {"positive_prices"}
    assert {c for c, _ in validate_bar(bar(volume=D("-1")), NOW)} == {"non_negative_volume"}
    assert {c for c, _ in validate_bar(bar(open_time=NOW + timedelta(hours=1)), NOW)} == {
        "timestamp_not_future"
    }
    assert {c for c, _ in validate_bar(bar(open_time=NOW - timedelta(minutes=90)), NOW)} == {
        "bar_alignment"
    }


def test_quote_validation() -> None:
    ok = QuoteRecord("BTC-USD", D("100"), D("101"), D("100.5"), NOW)
    assert validate_quote(ok, NOW) == []
    crossed = QuoteRecord("BTC-USD", D("102"), D("101"), D("100"), NOW)
    assert [c for c, _ in validate_quote(crossed, NOW)] == ["bid_not_above_ask"]
    negative = QuoteRecord("BTC-USD", D("-1"), D("101"), None, NOW)
    assert "positive_prices" in [c for c, _ in validate_quote(negative, NOW)]


def test_bar_completeness() -> None:
    assert is_complete(NOW - timedelta(hours=1), "1h", NOW)
    assert not is_complete(NOW - timedelta(minutes=59), "1h", NOW)
