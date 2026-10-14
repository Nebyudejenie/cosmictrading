"""Synthetic daily markets for library tests (seeded, deterministic; no edge by construction)."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from cosmic.research.library.engine import Market


def random_walk(
    key: str = "SYN",
    bars: int = 600,
    seed: int = 7,
    drift: float = 0.0004,
    vol: float = 0.03,
    start: date = date(2022, 1, 1),
) -> Market:
    rng = random.Random(seed)
    price = 100.0
    o, h, lo, c, v, d = [], [], [], [], [], []
    for i in range(bars):
        op = price
        ret = rng.gauss(drift, vol)
        cl = op * math.exp(ret)
        hi = max(op, cl) * math.exp(abs(rng.gauss(0, vol / 2)))
        low = min(op, cl) * math.exp(-abs(rng.gauss(0, vol / 2)))
        o.append(op)
        h.append(hi)
        lo.append(low)
        c.append(cl)
        v.append(1000 * math.exp(rng.gauss(0, 0.5)))
        d.append(start + timedelta(days=i))
        price = cl * math.exp(rng.gauss(0, vol / 10))  # small overnight gap
    return Market(key, tuple(d), tuple(o), tuple(h), tuple(lo), tuple(c), tuple(v))
