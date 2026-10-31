"""Synthetic bars for the catalog tests.  Fixture data, never market history."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

from axiom_forex.research.engine import CostModel
from axiom_forex.research.series import build_series, rows_from_rates

UTC = timezone.utc
START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)

#: A tight, made-up cost model: EUR/USD-like point and contract, one pip of spread
#: is *in the bars*, so the model adds only the stated slippage.
SAMPLE_COST = CostModel(
    point=0.00001,
    contract_size=100_000.0,
    account_currency="USD",
    quote_currency="USD",
    slippage_pips=0.2,
    commission_per_lot=0.0,
    swap_state="POINTS",
    swap_long=-0.5,
    swap_short=0.2,
)


def series_from_closes(
    closes: Sequence[float],
    *,
    symbol: str = "EURUSD.pro",
    timeframe: str = "15m",
    bar_seconds: int = 900,
    spread_points: int = 4,
    wick: float = 0.00003,
    source: str = "SAMPLE_DATA",
) -> Any:
    """One bar per close, with the previous close as the open."""

    rows = []
    previous = float(closes[0])
    for index, close in enumerate(closes):
        open_ = previous
        high = max(open_, close) + wick
        low = min(open_, close) - wick
        rows.append(
            {
                "timestamp": START + timedelta(seconds=bar_seconds * index),
                "open": f"{open_:.5f}",
                "high": f"{high:.5f}",
                "low": f"{low:.5f}",
                "close": f"{close:.5f}",
                "spread_points": spread_points,
                "tick_volume": 7,
                "source": source,
            }
        )
        previous = close
    return build_series(
        symbol, "EUR/USD", timeframe, bar_seconds, Decimal("0.00001"), rows_from_rates(rows)
    )


def random_walk(bars: int = 900, *, seed: int = 3, step: float = 0.00007, start: float = 1.10) -> list[float]:
    rng = random.Random(seed)
    price = start
    closes = []
    for _ in range(bars):
        price = round(price + rng.gauss(0.0, step), 5)
        closes.append(price)
    return closes


def trending(bars: int = 900, *, seed: int = 5, step: float = 0.00006, leg: int = 150) -> list[float]:
    """Alternating up and down legs, so both sides of a rule see a trend."""

    rng = random.Random(seed)
    price = 1.10
    closes = []
    for index in range(bars):
        drift = step * (1 if (index // leg) % 2 == 0 else -1)
        price = round(price + drift + rng.gauss(0.0, step), 5)
        closes.append(price)
    return closes
