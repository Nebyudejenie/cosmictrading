from __future__ import annotations

import time

from cosmic.core.ids import trade_code, uuid7, uuid7_timestamp_ms


def test_uuid7_version_variant_and_timestamp() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"
    assert before <= uuid7_timestamp_ms(value) <= after + 1


def test_uuid7_strictly_increasing_within_process() -> None:
    values = [uuid7() for _ in range(5000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_trade_code_format() -> None:
    code = trade_code("20261010")
    assert code.startswith("T-20261010-")
    suffix = code.rsplit("-", 1)[1]
    assert len(suffix) == 4 and not set(suffix) & set("ILOU")
