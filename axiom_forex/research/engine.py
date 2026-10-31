"""One year of history, every candidate, once, inside a ten-minute budget.

What this does, in order, for each symbol and timeframe it is given:

1. **Checks the history first.**  Bars, gaps, coverage, first/last timestamp.  A
   series that is not enough is reported as not enough -- it is never padded,
   interpolated, or replaced with something that resembles history.
2. **Computes every predeclared variant's signal once** over the whole series
   (see :mod:`axiom_forex.research.fastrules`), then turns signals into trades
   with the research fill convention.
3. **Splits chronologically** 60 / 20 / 20: train, validation, locked test.
   Nothing is chosen on the test window; the selection is made on validation and
   only the selected variant is then measured on the test window.
4. **Reports costs as costs.**  Spread comes from the bar's own quoted spread;
   slippage and commission carry their state (``ESTIMATE`` / ``ASSUMED_ZERO`` /
   ``UNKNOWN``); swap is included only when the terminal actually reported it.
   Anything that could not be priced is named in ``costs_incomplete`` rather than
   quietly dropped from the number.
5. **Stops when it has to.**  A wall-clock budget, a cancel check, and a progress
   callback.  Whatever did not run is listed with a reason, so a truncated run
   cannot be mistaken for a complete one.

Every number here is a *research estimate* on historical bars under stated
assumptions.  Nothing in this module claims a strategy is profitable, and nothing
in it can arm, size, or place anything.

The fill convention, stated once:

* a decision is taken on bar ``i``, using only bars ``<= i``;
* it fills at the **open of bar ``i + 1``**: ask for a buy, bid for a sell, plus
  slippage in the adverse direction;
* an opposing entry closes the open position and opens the new one at that same
  fill (a flip), which charges each leg its half-turn of spread;
* a position still open when a segment ends is closed at that segment's last bar
  and flagged ``FORCED`` -- it is counted, not hidden.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from axiom_forex.strategies.base import Signal
from axiom_forex.strategies.library.cards import Capabilities, StrategyCard
from axiom_forex.strategies.library.variants import GRID_VERSION, Variant, all_variants

from .fastrules import EXACT_FALLBACK_BUDGET, signals_for
from .series import Series, audit_coverage

#: Bar length per timeframe.  A timeframe not in here cannot be evaluated.
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

SEGMENTS: tuple[str, ...] = ("train", "validation", "test")

#: Chronological fractions: where a candidate may be looked at, where it is
#: chosen, and the window spent once at the end.
SPLIT = (0.6, 0.2, 0.2)

#: Default wall-clock budget.  Ten minutes is the requirement; the sweep keeps a
#: minute in hand to write the report rather than being killed mid-sentence.
DEFAULT_BUDGET_SECONDS = 540.0

#: Walk-forward chunks over the whole sample.  Parameters are fixed before these
#: run: a fold is a stability check, never a retuning opportunity.
WALK_FORWARD_FOLDS = 5

#: A variant with fewer closed trades than this on a window is not ranked on it.
MIN_TRADES_TO_RANK = 5

STATUS_COMPLETE = "COMPLETE"
STATUS_TIMEOUT = "TIMEOUT_PARTIAL"
STATUS_CANCELLED = "CANCELLED_PARTIAL"

#: Cost provenance labels.  These travel with every number derived from them.
OBSERVED = "OBSERVED"
ESTIMATE = "ESTIMATE"
ASSUMED_ZERO = "ASSUMED_ZERO"
UNAVAILABLE = "UNAVAILABLE"
UNKNOWN = "UNKNOWN"

SPREAD_FROM_BARS = "BROKER_RATES_QUOTED_SPREAD"

# Exit reasons.
SIGNAL_EXIT = "SIGNAL_EXIT"
OPPOSING_ENTRY = "OPPOSING_ENTRY"
FORCED = "FORCED"

#: What a report is allowed to say about itself.  There is no profitable branch.
CLAIM_NOTE = (
    "Historical research estimate on broker bars under the stated cost assumptions. "
    "Not a forecast, not a promise of profit, and not evidence that a strategy is "
    "ready for live trading."
)


class SweepCancelled(Exception):
    """Raised internally when the operator cancels; never shown to the UI raw."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


@dataclass(frozen=True)
class CostModel:
    """What a round turn costs, and how well each part of that is known.

    Monetary values are per one lot.  Price-derived amounts are in the pair's
    *quote* currency, which is what the market quotes.  Account-currency items
    (commission, swap) are included only when the account currency is the quote
    currency or a conversion was supplied; otherwise they are excluded *and named*.
    """

    point: float
    contract_size: float
    account_currency: str = UNKNOWN
    quote_currency: str = UNKNOWN
    slippage_pips: float = 0.2
    slippage_state: str = ESTIMATE
    commission_per_lot: float = 0.0
    commission_state: str = ASSUMED_ZERO
    swap_long: float | None = None
    swap_short: float | None = None
    swap_state: str = UNKNOWN
    rollover_utc_hours: tuple[int, ...] = ()
    conversion_rate: float | None = None  # account units per one quote-currency unit

    # -- derivation -------------------------------------------------------

    @property
    def conversion_known(self) -> bool:
        if self.conversion_rate is not None:
            return True
        return bool(self.account_currency) and self.account_currency == self.quote_currency

    def rate(self) -> float:
        if self.conversion_rate is not None:
            return float(self.conversion_rate)
        return 1.0 if self.conversion_known else 0.0

    @property
    def slippage_price(self) -> float:
        return max(0.0, float(self.slippage_pips)) * float(self.point or 0.0)

    def pip_value(self, lot: float) -> float:
        """Quote-currency value of one point on ``lot`` lots."""

        return float(self.point or 0.0) * float(self.contract_size or 0.0) * float(lot)

    def commission_quote(self, lot: float) -> float | None:
        """Round-turn commission in quote currency, or ``None`` if unpriceable."""

        amount = max(0.0, float(self.commission_per_lot)) * float(lot) * 2.0
        if amount == 0.0:
            return 0.0
        if not self.conversion_known or not self.rate():
            return None
        return amount / self.rate()

    def swap_quote(self, direction: int, lot: float, nights: int) -> float | None:
        """Swap accrued over ``nights`` rollovers, in quote currency.

        Points-mode swaps are exact: points x point x contract x lot.  A
        currency-mode swap is reported in the account currency, so it converts only
        when the conversion is known.  Interest-mode swaps cannot be priced at all
        without the two legs' rates, which this run does not have.
        """

        if nights <= 0 or self.swap_state in (UNKNOWN, UNAVAILABLE):
            return 0.0 if nights <= 0 else None
        if self.swap_state == "INTEREST_MODE":
            return None
        raw = self.swap_long if direction > 0 else self.swap_short
        if raw is None:
            return None
        amount = float(raw) * float(lot) * float(nights)
        if self.swap_state == "POINTS":
            return amount * float(self.point or 0.0) * float(self.contract_size or 0.0)
        if self.swap_state == "CURRENCY":
            if not self.conversion_known or not self.rate():
                return None
            return amount / self.rate()
        if self.swap_state == "DISABLED":
            return 0.0
        return None

    # -- reporting --------------------------------------------------------

    def labels(self) -> dict[str, str]:
        return {
            "spread": SPREAD_FROM_BARS,
            "slippage": self.slippage_state,
            "commission": self.commission_state,
            "swap": self.swap_state,
            "conversion": OBSERVED if self.conversion_known else UNKNOWN,
            "rollover": OBSERVED if self.rollover_utc_hours else UNKNOWN,
        }

    def incomplete(self) -> tuple[str, ...]:
        """The parts of the cost stack that are not observed, with their effect."""

        out: list[str] = []
        if self.slippage_state != OBSERVED:
            out.append(
                f"slippage is an {self.slippage_state.lower()} of "
                f"{self.slippage_pips:g} pip(s) per side, not a measured fill"
            )
        if self.commission_state != OBSERVED:
            out.append(
                "commission is "
                + (
                    "assumed to be zero because the broker did not report it"
                    if self.commission_state == ASSUMED_ZERO
                    else f"{self.commission_state.lower()} and is not in the net figure"
                )
            )
        if self.swap_state in (UNKNOWN, UNAVAILABLE):
            out.append(
                "swap is unknown, so results are gross of financing and a position "
                "held for many days is under-charged"
            )
        elif self.swap_state == "INTEREST_MODE":
            out.append(
                "the broker reports swap in interest mode, which needs per-leg rates "
                "this run did not have, so swap is excluded"
            )
        if not self.conversion_known and (
            self.commission_state != ASSUMED_ZERO
            or self.swap_state not in (UNKNOWN, UNAVAILABLE)
        ):
            out.append(
                f"the account currency ({self.account_currency or UNKNOWN}) is not the "
                f"pair's quote currency ({self.quote_currency or UNKNOWN}), so account-"
                "currency costs could not be converted and are excluded"
            )
        if not self.rollover_utc_hours:
            out.append(
                "the broker's rollover cut-off was not readable, so rollovers are "
                "counted at the 00:00 UTC day boundary -- the number of nights is "
                "right, the exact charge instant may be hours off"
            )
        return tuple(out)

    def audit_view(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "contract_size": self.contract_size,
            "account_currency": self.account_currency or UNKNOWN,
            "quote_currency": self.quote_currency or UNKNOWN,
            "slippage_pips": self.slippage_pips,
            "commission_per_lot": self.commission_per_lot,
            "swap_long": self.swap_long,
            "swap_short": self.swap_short,
            "swap_state": self.swap_state,
            "rollover_utc_hours": list(self.rollover_utc_hours),
            "labels": self.labels(),
            "incomplete": list(self.incomplete()),
        }

    def scaled(self, multiplier: float) -> "CostModel":
        """The same model with every *assumed* money amount multiplied.

        Used for the sensitivity run: if doubling the assumed slippage and
        commission erases the result, the result was the assumption.
        """

        return CostModel(
            point=self.point,
            contract_size=self.contract_size,
            account_currency=self.account_currency,
            quote_currency=self.quote_currency,
            slippage_pips=self.slippage_pips * float(multiplier),
            slippage_state=self.slippage_state,
            commission_per_lot=self.commission_per_lot * float(multiplier),
            commission_state=self.commission_state,
            swap_long=self.swap_long,
            swap_short=self.swap_short,
            swap_state=self.swap_state,
            rollover_utc_hours=self.rollover_utc_hours,
            conversion_rate=self.conversion_rate,
        )


@dataclass(frozen=True)
class Trade:
    """One completed round turn, with costs separated from the gross move."""

    variant_id: str
    symbol: str
    timeframe: str
    segment: str
    direction: int  # +1 long, -1 short
    entry_at: str
    exit_at: str
    entry_price: float  # the price actually filled: bid/ask, slippage applied
    exit_price: float
    entry_mid: float
    exit_mid: float
    gross_quote: float
    spread_slippage_quote: float
    commission_quote: float | None
    swap_quote: float | None
    net_quote: float
    gross_pips: float
    cost_pips: float
    net_pips: float
    nights: int
    exit_reason: str
    bars_held: int
    notional_quote: float

    @property
    def net_return(self) -> float:
        return self.net_quote / self.notional_quote if self.notional_quote > 0 else 0.0

    @property
    def gross_return(self) -> float:
        return self.gross_quote / self.notional_quote if self.notional_quote > 0 else 0.0

    def cell(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "segment": self.segment,
            "direction": "LONG" if self.direction > 0 else "SHORT",
            "entry_at": self.entry_at,
            "exit_at": self.exit_at,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "gross_pips": round(self.gross_pips, 2),
            "cost_pips": round(self.cost_pips, 2),
            "net_pips": round(self.net_pips, 2),
            "commission_quote": self.commission_quote,
            "swap_quote": self.swap_quote,
            "net_quote": round(self.net_quote, 6),
            "nights": self.nights,
            "exit_reason": self.exit_reason,
            "bars_held": self.bars_held,
            "net_return": round(self.net_return, 6),
        }


@dataclass(frozen=True)
class SegmentStats:
    """Trades, return, drawdown and costs for one window."""

    segment: str
    bars: int
    trades: int
    long_trades: int
    short_trades: int
    winners: int
    gross_return: float
    cost_return: float
    net_return: float
    max_drawdown: float
    gross_pips: float
    cost_pips: float
    net_pips: float
    nights: int
    forced_exits: int
    bars_held: int
    costs_incomplete: tuple[str, ...] = field(default=())

    @property
    def win_rate(self) -> float:
        return self.winners / self.trades if self.trades else 0.0

    @property
    def avg_net_pips(self) -> float:
        return self.net_pips / self.trades if self.trades else 0.0

    @property
    def avg_bars_held(self) -> float:
        return self.bars_held / self.trades if self.trades else 0.0

    def audit_view(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "bars": self.bars,
            "trades": self.trades,
            "long_trades": self.long_trades,
            "short_trades": self.short_trades,
            "winners": self.winners,
            "win_rate": round(self.win_rate, 4),
            "gross_return": round(self.gross_return, 6),
            "cost_return": round(self.cost_return, 6),
            "net_return": round(self.net_return, 6),
            "max_drawdown": round(self.max_drawdown, 6),
            "gross_pips": round(self.gross_pips, 2),
            "cost_pips": round(self.cost_pips, 2),
            "net_pips": round(self.net_pips, 2),
            "avg_net_pips": round(self.avg_net_pips, 3),
            "avg_bars_held": round(self.avg_bars_held, 1),
            "nights": self.nights,
            "forced_exits": self.forced_exits,
            "costs_incomplete": list(self.costs_incomplete),
        }


def _empty_stats(segment: str, bars: int, incomplete: Sequence[str]) -> SegmentStats:
    return SegmentStats(
        segment=segment,
        bars=bars,
        trades=0,
        long_trades=0,
        short_trades=0,
        winners=0,
        gross_return=0.0,
        cost_return=0.0,
        net_return=0.0,
        max_drawdown=0.0,
        gross_pips=0.0,
        cost_pips=0.0,
        net_pips=0.0,
        nights=0,
        forced_exits=0,
        bars_held=0,
        costs_incomplete=tuple(incomplete),
    )


def segment_stats(
    segment: str,
    trades: Sequence[Trade],
    bars: int,
    incomplete: Sequence[str] = (),
) -> SegmentStats:
    """Aggregate trades into the numbers the report shows, drawdown included."""

    if not trades:
        return _empty_stats(segment, bars, incomplete)
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for trade in trades:
        equity += trade.net_return
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return SegmentStats(
        segment=segment,
        bars=bars,
        trades=len(trades),
        long_trades=sum(1 for trade in trades if trade.direction > 0),
        short_trades=sum(1 for trade in trades if trade.direction < 0),
        winners=sum(1 for trade in trades if trade.net_quote > 0),
        gross_return=sum(trade.gross_return for trade in trades),
        cost_return=sum(trade.gross_return - trade.net_return for trade in trades),
        net_return=equity,
        max_drawdown=drawdown,
        gross_pips=sum(trade.gross_pips for trade in trades),
        cost_pips=sum(trade.cost_pips for trade in trades),
        net_pips=sum(trade.net_pips for trade in trades),
        nights=sum(trade.nights for trade in trades),
        forced_exits=sum(1 for trade in trades if trade.exit_reason == FORCED),
        bars_held=sum(trade.bars_held for trade in trades),
        costs_incomplete=tuple(incomplete),
    )


# -- simulation -------------------------------------------------------------


def _fill_price(series: Series, index: int, direction: int, cost: CostModel, side: str) -> float:
    """Buy pays the ask, sell receives the bid, both with slippage against us."""

    slip = cost.slippage_price
    if direction > 0:
        return series.ask(index, side) + slip
    return series.bid(index, side) - slip


def _mid(series: Series, index: int, side: str) -> float:
    base = getattr(series, side)[index]
    return base + series.spread[index] / 2.0


def _days_between(start: datetime, end: datetime) -> int:
    return max(0, (end.date() - start.date()).days)


@dataclass(frozen=True)
class _Open:
    direction: int
    index: int
    price: float
    mid: float
    when: datetime
    notional: float


def simulate(
    series: Series,
    signals: Sequence[Signal],
    *,
    variant_id: str,
    segment: str,
    cost: CostModel,
    lot: float,
    start: int,
    end: int,
) -> tuple[tuple[Trade, ...], int]:
    """Turn a signal per bar into round turns inside ``[start, end)``.

    Returns the trades and the count of entry signals that had no bar left to fill
    on (they are counted, not silently dropped).
    """

    point = float(cost.point or series.point or 0.0)
    contract = float(cost.contract_size or 0.0)
    pip_value = point * contract * float(lot)
    unfilled = 0
    trades: list[Trade] = []
    holding: _Open | None = None
    start = max(0, int(start))
    end = min(len(series), int(end))

    def close(
        position: _Open, index: int, side: str, reason: str
    ) -> Trade:
        exit_price = _fill_price(series, index, -position.direction, cost, side)
        exit_mid = _mid(series, index, side)
        nights = _days_between(position.when, series.timestamps[index])
        gross_quote = (
            position.direction * (exit_mid - position.mid) * contract * float(lot)
        )
        filled_quote = (
            position.direction * (exit_price - position.price) * contract * float(lot)
        )
        commission = cost.commission_quote(lot)
        swap = cost.swap_quote(position.direction, lot, nights)
        net_quote = filled_quote - (commission or 0.0) - (swap or 0.0)
        extra_pips = ((commission or 0.0) + (swap or 0.0)) / pip_value if pip_value else 0.0
        return Trade(
            variant_id=variant_id,
            symbol=series.symbol,
            timeframe=series.timeframe,
            segment=segment,
            direction=position.direction,
            entry_at=_iso(position.when),
            exit_at=_iso(series.timestamps[index]),
            entry_price=position.price,
            exit_price=exit_price,
            entry_mid=position.mid,
            exit_mid=exit_mid,
            gross_quote=gross_quote,
            spread_slippage_quote=gross_quote - filled_quote,
            commission_quote=commission,
            swap_quote=swap,
            net_quote=net_quote,
            gross_pips=(position.direction * (exit_mid - position.mid) / point) if point else 0.0,
            cost_pips=((gross_quote - filled_quote) / pip_value) if pip_value else 0.0,
            net_pips=(
                (position.direction * (exit_price - position.price) / point) - extra_pips
                if point
                else 0.0
            ),
            nights=nights,
            exit_reason=reason,
            bars_held=max(0, index - position.index),
            notional_quote=position.notional,
        )

    for index in range(start, end):
        signal = signals[index]
        if signal == Signal.HOLD:
            continue
        wanted = 0
        if signal == Signal.BUY:
            wanted = 1
        elif signal == Signal.SELL:
            wanted = -1
        fill_index = index + 1
        can_fill = fill_index < end

        if holding is None:
            if wanted and can_fill:
                price = _fill_price(series, fill_index, wanted, cost, "o")
                mid = _mid(series, fill_index, "o")
                holding = _Open(
                    direction=wanted,
                    index=fill_index,
                    price=price,
                    mid=mid,
                    when=series.timestamps[fill_index],
                    notional=abs(mid * contract * float(lot)),
                )
            elif wanted:
                unfilled += 1
            continue

        if signal == Signal.EXIT or (wanted and wanted != holding.direction):
            reason = SIGNAL_EXIT if signal == Signal.EXIT else OPPOSING_ENTRY
            if can_fill:
                trades.append(close(holding, fill_index, "o", reason))
                position_closed_at = fill_index
                holding = None
            else:
                # No bar left in the segment: settle on the segment's last bar.
                trades.append(close(holding, end - 1, "c", FORCED))
                holding = None
                continue
            if wanted:
                price = _fill_price(series, position_closed_at, wanted, cost, "o")
                mid = _mid(series, position_closed_at, "o")
                holding = _Open(
                    direction=wanted,
                    index=position_closed_at,
                    price=price,
                    mid=mid,
                    when=series.timestamps[position_closed_at],
                    notional=abs(mid * contract * float(lot)),
                )

    if holding is not None and end > start:
        trades.append(close(holding, end - 1, "c", FORCED))
    return tuple(trades), unfilled


# -- dataset identity ------------------------------------------------------


def series_identity(series: Series) -> tuple[str, str]:
    """A dataset id and a content fingerprint for one series.

    The id says where it came from; the fingerprint says exactly which bars it was.
    Two runs on the same bars produce the same fingerprint, which is what lets a
    test window be spent exactly once per dataset.
    """

    digest = hashlib.sha256()
    digest.update(str(series.symbol).encode("utf-8"))
    digest.update(str(series.timeframe).encode("utf-8"))
    for index in range(len(series)):
        digest.update(series.timestamps[index].isoformat().encode("ascii"))
        digest.update(f"{series.o[index]:.10g},{series.h[index]:.10g}".encode("ascii"))
        digest.update(f"{series.l[index]:.10g},{series.c[index]:.10g}".encode("ascii"))
        digest.update(f"{series.spread[index]:.10g}".encode("ascii"))
    fingerprint = digest.hexdigest()[:32]
    dataset_id = "terminal:" + ":".join(
        [
            str(series.symbol),
            str(series.timeframe),
            f"{len(series)}bars",
            series.timestamps[0].strftime("%Y%m%d") if len(series) else "empty",
            series.timestamps[-1].strftime("%Y%m%d") if len(series) else "empty",
        ]
    )
    return dataset_id, fingerprint


# -- report ----------------------------------------------------------------


@dataclass(frozen=True)
class CoverageEntry:
    """Which symbols and timeframes had usable history, and why not."""

    symbol: str
    timeframe: str
    bars: int
    first_at: str
    last_at: str
    usable: bool
    reason: str
    coverage_fraction: float
    gaps: int
    largest_gap_seconds: float
    price_basis: str
    notes: tuple[str, ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "usable": self.usable,
            "reason": self.reason,
            "coverage_fraction": round(self.coverage_fraction, 4),
            "gaps": self.gaps,
            "largest_gap_seconds": self.largest_gap_seconds,
            "price_basis": self.price_basis,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class VariantResult:
    """One variant on one symbol and timeframe: three windows, one verdict-free row."""

    variant_id: str
    card_id: str
    family: str
    symbol: str
    timeframe: str
    status: str  # MEASURED | UNAVAILABLE | FAILED | NOT_RUN
    reason: str
    train: SegmentStats | None
    validation: SegmentStats | None
    test: SegmentStats | None
    #: Which directions the rule actually asked for on this history.
    directions: str = "NONE"
    params: Mapping[str, str] = field(default_factory=dict)
    test_disclosure: str = ""

    @property
    def measured(self) -> bool:
        return self.status == "MEASURED"

    def audit_view(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "card_id": self.card_id,
            "family": self.family,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": self.status,
            "reason": self.reason,
            "params": dict(self.params),
            "train": self.train.audit_view() if self.train else None,
            "validation": self.validation.audit_view() if self.validation else None,
            "test": self.test.audit_view() if self.test else None,
            "directions": self.directions,
            "test_disclosure": self.test_disclosure,
        }


@dataclass(frozen=True)
class WalkForward:
    """Stability of the selected variant across later chunks of the same sample."""

    folds: tuple[dict[str, Any], ...]
    positive_folds: int
    median_net_return: float

    @property
    def positive_fraction(self) -> float:
        return self.positive_folds / len(self.folds) if self.folds else 0.0

    def audit_view(self) -> dict[str, Any]:
        return {
            "folds": [dict(item) for item in self.folds],
            "positive_folds": self.positive_folds,
            "positive_fraction": round(self.positive_fraction, 4),
            "median_net_return": round(self.median_net_return, 6),
            "note": (
                "Folds are later chunks of the same history. The parameters were fixed "
                "before the folds ran, so a fold is a stability check, not a retune."
            ),
        }


@dataclass(frozen=True)
class SeriesResult:
    """Everything measured for one symbol and timeframe."""

    symbol: str
    timeframe: str
    coverage: CoverageEntry
    dataset_id: str
    dataset_fingerprint: str
    bars: int
    train_bars: int
    validation_bars: int
    test_bars: int
    costs: Mapping[str, Any]
    variants: tuple[VariantResult, ...]
    selected_variant_id: str = ""
    selection_reason: str = ""
    walk_forward: WalkForward | None = None
    sensitivity: tuple[dict[str, Any], ...] = ()
    signal_fallbacks: int = 0
    signal_bars: int = 0
    elapsed_seconds: float = 0.0

    @property
    def selected(self) -> VariantResult | None:
        for item in self.variants:
            if item.variant_id == self.selected_variant_id:
                return item
        return None

    @property
    def measured(self) -> int:
        return sum(1 for item in self.variants if item.measured)

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "coverage": self.coverage.audit_view(),
            "dataset_id": self.dataset_id,
            "dataset_fingerprint": self.dataset_fingerprint,
            "bars": self.bars,
            "windows": {
                "train_bars": self.train_bars,
                "validation_bars": self.validation_bars,
                "test_bars": self.test_bars,
                "split": list(SPLIT),
            },
            "costs": dict(self.costs),
            "variants": [item.audit_view() for item in self.variants],
            "measured": self.measured,
            "selected_variant_id": self.selected_variant_id,
            "selection_reason": self.selection_reason,
            "walk_forward": self.walk_forward.audit_view() if self.walk_forward else None,
            "sensitivity": [dict(item) for item in self.sensitivity],
            "signal_fallbacks": self.signal_fallbacks,
            "signal_bars": self.signal_bars,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


@dataclass(frozen=True)
class SweepReport:
    """The whole run: what was measured, what was not, and why."""

    status: str
    started_at: str
    finished_at: str
    elapsed_seconds: float
    budget_seconds: float
    series: tuple[SeriesResult, ...]
    not_run: tuple[dict[str, str], ...]
    failures: tuple[dict[str, str], ...]
    notes: tuple[str, ...]
    lot_size: float
    window_days: int
    claim: str = CLAIM_NOTE

    @property
    def complete(self) -> bool:
        return self.status == STATUS_COMPLETE

    def table(self) -> list[dict[str, Any]]:
        """One row per measured variant per series, in the report's own words."""

        rows: list[dict[str, Any]] = []
        for result in self.series:
            for item in result.variants:
                row: dict[str, Any] = {
                    "symbol": item.symbol,
                    "timeframe": item.timeframe,
                    "strategy": item.variant_id,
                    "family": item.family,
                    "status": item.status,
                    "directions": item.directions,
                    "selected_for_test": item.variant_id == result.selected_variant_id,
                }
                if item.validation is not None:
                    row["validation_trades"] = item.validation.trades
                    row["validation_net_return"] = round(item.validation.net_return, 6)
                    row["validation_max_drawdown"] = round(item.validation.max_drawdown, 6)
                    row["validation_cost_return"] = round(item.validation.cost_return, 6)
                if item.test is not None:
                    row["test_trades"] = item.test.trades
                    row["test_net_return"] = round(item.test.net_return, 6)
                    row["test_max_drawdown"] = round(item.test.max_drawdown, 6)
                    row["test_cost_return"] = round(item.test.cost_return, 6)
                elif item.test_disclosure:
                    row["test_note"] = item.test_disclosure
                rows.append(row)
        return rows

    def summary(self) -> dict[str, Any]:
        """Counts a UI can show without re-reading every row."""

        measured = sum(result.measured for result in self.series)
        total = sum(len(result.variants) for result in self.series)
        usable = sum(1 for result in self.series if result.coverage.usable)
        return {
            "status": self.status,
            "series": len(self.series),
            "series_with_usable_history": usable,
            "variants_measured": measured,
            "variants_total": total,
            "not_run": len(self.not_run),
            "failures": len(self.failures),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "budget_seconds": self.budget_seconds,
            "claim": self.claim,
        }

    def audit_view(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "budget_seconds": self.budget_seconds,
            "lot_size": self.lot_size,
            "window_days": self.window_days,
            "series": [item.audit_view() for item in self.series],
            "not_run": [dict(item) for item in self.not_run],
            "failures": [dict(item) for item in self.failures],
            "notes": list(self.notes),
            "claim": self.claim,
        }

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe form for the local report store.  Contains no credentials."""

        return self.audit_view()


# -- the sweep -------------------------------------------------------------


@dataclass
class _Budget:
    seconds: float
    clock: Callable[[], float]
    cancel: Callable[[], bool] | None
    started: float = field(default_factory=float)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def spent(self) -> float:
        return self.clock() - self.started

    def exhausted(self) -> bool:
        return self.spent() >= max(0.0, self.seconds)

    def cancelled(self) -> bool:
        return bool(self.cancel and self.cancel())


def _windows(bars: int) -> tuple[tuple[int, int], ...]:
    """Contiguous 60/20/20 index windows: train, validation, test."""

    train_end = int(bars * SPLIT[0])
    validation_end = train_end + int(bars * SPLIT[1])
    return ((0, train_end), (train_end, validation_end), (validation_end, bars))


def _walk_forward(
    series: Series,
    signals: Sequence[Signal],
    *,
    variant_id: str,
    cost: CostModel,
    lot: float,
    folds: int,
) -> WalkForward:
    """Split the whole sample into later chunks and measure the fixed parameters."""

    count = max(1, int(folds))
    size = len(series) // count
    rows: list[dict[str, Any]] = []
    returns: list[float] = []
    for number in range(count):
        start = number * size
        end = len(series) if number == count - 1 else (number + 1) * size
        if end - start < 2:
            continue
        trades, _unfilled = simulate(
            series,
            signals,
            variant_id=variant_id,
            segment=f"fold_{number + 1}",
            cost=cost,
            lot=lot,
            start=start,
            end=end,
        )
        stats = segment_stats(f"fold_{number + 1}", trades, end - start)
        returns.append(stats.net_return)
        rows.append(
            {
                "fold": number + 1,
                "from": _iso(series.timestamps[start]) if start < len(series) else "",
                "to": _iso(series.timestamps[end - 1]),
                "trades": stats.trades,
                "net_return": round(stats.net_return, 6),
                "max_drawdown": round(stats.max_drawdown, 6),
            }
        )
    positive = sum(1 for value in returns if value > 0)
    ordered = sorted(returns)
    median = 0.0
    if ordered:
        middle = len(ordered) // 2
        median = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2.0
        )
    return WalkForward(folds=tuple(rows), positive_folds=positive, median_net_return=median)


def _capabilities_for(costs: Iterable[CostModel]) -> Capabilities:
    """Swap rates count as available only if every series could actually price them."""

    models = list(costs)
    priced = bool(models) and all(
        model.swap_state in ("POINTS", "CURRENCY", "DISABLED") for model in models
    )
    return Capabilities(swap_rates=priced)


def run_sweep(
    series_by_key: Mapping[tuple[str, str], Series],
    *,
    costs: Mapping[str, CostModel],
    lot_size: float = 0.10,
    variants: Sequence[Variant] | None = None,
    window_days: int = 365,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
    min_bars: int = 500,
    min_coverage: float = 0.90,
    sensitivity_multipliers: Sequence[float] = (1.0, 2.0),
    walk_forward_folds: int = WALK_FORWARD_FOLDS,
    now: datetime | None = None,
) -> SweepReport:
    """Measure every candidate on every usable series, or say what stopped it.

    ``series_by_key`` is ``{(symbol, timeframe): Series}``.  Series that are not
    usable are reported and skipped: nothing is invented to fill them in.
    """

    started = now or _utc_now()
    budget = _Budget(seconds=float(budget_seconds), clock=clock, cancel=cancel)
    candidates = tuple(variants) if variants is not None else all_variants()
    capabilities = _capabilities_for(costs.values())
    ordered = sorted(series_by_key.items(), key=lambda item: (len(item[1]), item[0]))
    units = sum(
        len(candidates)
        for (symbol, _timeframe), item in ordered
        if len(item) and symbol in costs
    )
    done = 0

    results: list[SeriesResult] = []
    not_run: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    notes: list[str] = []

    def report_progress(label: str) -> None:
        if progress is None:
            return
        fraction = (done / units) if units else 1.0
        progress(min(1.0, fraction), label)

    for (symbol, timeframe), series in ordered:
        key = {"symbol": symbol, "timeframe": timeframe}
        cost = costs.get(symbol)
        if cost is None:
            not_run.append(
                dict(
                    key,
                    reason="no_cost_model_for_symbol",
                    detail="the terminal gave no point size or contract size for this symbol",
                )
            )
            continue
        if timeframe not in TIMEFRAME_SECONDS:
            not_run.append(
                dict(key, reason="unknown_timeframe", detail="the bar length is not declared")
            )
            continue
        if budget.cancelled():
            not_run.append(dict(key, reason="cancelled", detail="the operator cancelled the run"))
            continue
        if budget.exhausted():
            not_run.append(
                dict(
                    key,
                    reason="budget_exhausted",
                    detail=f"{budget.spent():.0f}s of {budget.seconds:.0f}s used before this series",
                )
            )
            continue

        series_started = clock()
        coverage = audit_coverage(
            series,
            window_days=window_days,
            min_bars=min_bars,
            min_coverage=min_coverage,
            now=started,
        )
        entry = CoverageEntry(
            symbol=symbol,
            timeframe=timeframe,
            bars=len(series),
            first_at=_iso(series.timestamps[0]) if len(series) else "",
            last_at=_iso(series.timestamps[-1]) if len(series) else "",
            usable=coverage.usable,
            reason=coverage.reason,
            coverage_fraction=coverage.coverage_fraction,
            gaps=coverage.gaps,
            largest_gap_seconds=coverage.largest_gap_seconds,
            price_basis=series.price_basis,
            notes=tuple(coverage.notes) + tuple(series.notes),
        )
        dataset_id, fingerprint = series_identity(series)
        (train_window, validation_window, test_window) = _windows(len(series))

        if not coverage.usable:
            results.append(
                SeriesResult(
                    symbol=symbol,
                    timeframe=timeframe,
                    coverage=entry,
                    dataset_id=dataset_id,
                    dataset_fingerprint=fingerprint,
                    bars=len(series),
                    train_bars=0,
                    validation_bars=0,
                    test_bars=0,
                    costs=cost.audit_view(),
                    variants=tuple(
                        VariantResult(
                            variant_id=item.variant_id,
                            card_id=item.card_id,
                            family=item.card.family,
                            symbol=symbol,
                            timeframe=timeframe,
                            status="NOT_RUN",
                            reason=f"history_not_usable:{coverage.reason}",
                            train=None,
                            validation=None,
                            test=None,
                            directions="UNKNOWN",
                            params={name: str(value) for name, value in item.parameters.items()},
                        )
                        for item in candidates
                    ),
                    selection_reason="history not usable: " + (coverage.reason or "unknown"),
                    elapsed_seconds=clock() - series_started,
                )
            )
            continue

        measured: list[VariantResult] = []
        validation_rank: list[tuple[float, int, str]] = []
        fallbacks = 0

        for variant in candidates:
            label = f"{symbol} {timeframe} {variant.variant_id}"
            if budget.cancelled():
                not_run.append(
                    dict(
                        key,
                        variant_id=variant.variant_id,
                        reason="cancelled",
                        detail="the operator cancelled the run",
                    )
                )
                continue
            if budget.exhausted():
                not_run.append(
                    dict(
                        key,
                        variant_id=variant.variant_id,
                        reason="budget_exhausted",
                        detail=f"{budget.spent():.0f}s of {budget.seconds:.0f}s used",
                    )
                )
                continue
            unavailable = _unavailable_reason(variant.card, timeframe, capabilities)
            if unavailable:
                measured.append(
                    VariantResult(
                        variant_id=variant.variant_id,
                        card_id=variant.card_id,
                        family=variant.card.family,
                        symbol=symbol,
                        timeframe=timeframe,
                        status="UNAVAILABLE",
                        reason=unavailable,
                        train=None,
                        validation=None,
                        test=None,
                        directions="UNKNOWN",
                        params={name: str(value) for name, value in variant.parameters.items()},
                    )
                )
                done += 1
                report_progress(label + " unavailable")
                continue

            try:
                outcome = signals_for(series, variant)
            except Exception as exc:  # a broken candidate is reported, never hidden
                failures.append(
                    dict(
                        key,
                        variant_id=variant.variant_id,
                        reason="signals_failed",
                        detail=type(exc).__name__,
                    )
                )
                measured.append(
                    VariantResult(
                        variant_id=variant.variant_id,
                        card_id=variant.card_id,
                        family=variant.card.family,
                        symbol=symbol,
                        timeframe=timeframe,
                        status="FAILED",
                        reason=f"the rule raised {type(exc).__name__} on this history",
                        train=None,
                        validation=None,
                        test=None,
                        directions="UNKNOWN",
                        params={name: str(value) for name, value in variant.parameters.items()},
                    )
                )
                done += 1
                report_progress(label + " failed")
                continue

            fallbacks += outcome.fallbacks
            if outcome.fallbacks:
                fraction = outcome.fallback_fraction
                if fraction > EXACT_FALLBACK_BUDGET:
                    notes.append(
                        f"{symbol} {timeframe}: {outcome.fallbacks} of {outcome.bars} bars "
                        f"({fraction:.1%}) were decided by the exact rule rather than the "
                        f"fast path, above the {EXACT_FALLBACK_BUDGET:.0%} budget -- the "
                        "series has unusually many tied values"
                    )

            stats: dict[str, SegmentStats] = {}
            for name, (start, end) in zip(SEGMENTS, (train_window, validation_window, test_window)):
                trades, _unfilled = simulate(
                    series,
                    outcome.signals,
                    variant_id=variant.variant_id,
                    segment=name,
                    cost=cost,
                    lot=lot_size,
                    start=start,
                    end=end,
                )
                stats[name] = segment_stats(
                    name, trades, end - start, incomplete=cost.incomplete()
                )

            measured.append(
                VariantResult(
                    variant_id=variant.variant_id,
                    card_id=variant.card_id,
                    family=variant.card.family,
                    symbol=symbol,
                    timeframe=timeframe,
                    status="MEASURED",
                    reason="",
                    train=stats["train"],
                    validation=stats["validation"],
                    test=None,
                    directions=_directions(outcome.signals),
                    params={name: str(value) for name, value in variant.parameters.items()},
                    test_disclosure=(
                        "The locked test window is measured only for the variant chosen "
                        "on validation, so this row is not comparable on test."
                    ),
                )
            )
            if stats["validation"].trades >= MIN_TRADES_TO_RANK:
                validation_rank.append(
                    (stats["validation"].net_return, stats["validation"].trades, variant.variant_id)
                )
            done += 1
            report_progress(label)

        selected_id = ""
        selection_reason = ""
        walk: WalkForward | None = None
        sensitivity: list[dict[str, Any]] = []
        chosen_signals: Sequence[Signal] | None = None

        if validation_rank:
            validation_rank.sort(reverse=True)
            selected_id = validation_rank[0][2]
            chosen = next(item for item in candidates if item.variant_id == selected_id)
            try:
                chosen_signals = signals_for(series, chosen).signals
            except Exception as exc:  # pragma: no cover - already succeeded above
                failures.append(
                    dict(
                        key,
                        variant_id=selected_id,
                        reason="signals_failed_on_reselect",
                        detail=type(exc).__name__,
                    )
                )
            # The test window is measured now, once, for the selected variant only.
            start, end = test_window
            trades, _unfilled = simulate(
                series,
                chosen_signals,
                variant_id=selected_id,
                segment="test",
                cost=cost,
                lot=lot_size,
                start=start,
                end=end,
            )
            test_stats = segment_stats("test", trades, end - start, incomplete=cost.incomplete())
            selection_reason = (
                "chosen on validation only: highest validation net return "
                f"({validation_rank[0][0]:+.4%}) among variants with at least "
                f"{MIN_TRADES_TO_RANK} validation trades; the test window was then "
                "measured once for this variant alone"
            )
            measured = [
                replace(item, test=test_stats, test_disclosure="")
                if item.variant_id == selected_id
                else item
                for item in measured
            ]
            if chosen_signals is not None:
                walk = _walk_forward(
                    series,
                    chosen_signals,
                    variant_id=selected_id,
                    cost=cost,
                    lot=lot_size,
                    folds=walk_forward_folds,
                )
                for multiplier in sensitivity_multipliers:
                    scaled = cost.scaled(multiplier)
                    start_v, end_v = validation_window
                    trades_v, _u = simulate(
                        series,
                        chosen_signals,
                        variant_id=selected_id,
                        segment="validation",
                        cost=scaled,
                        lot=lot_size,
                        start=start_v,
                        end=end_v,
                    )
                    scaled_stats = segment_stats(
                        "validation", trades_v, end_v - start_v, incomplete=scaled.incomplete()
                    )
                    sensitivity.append(
                        {
                            "cost_multiplier": multiplier,
                            "slippage_pips": round(scaled.slippage_pips, 4),
                            "commission_per_lot": round(scaled.commission_per_lot, 4),
                            "trades": scaled_stats.trades,
                            "net_return": round(scaled_stats.net_return, 6),
                            "window": "validation",
                        }
                    )
        else:
            selection_reason = (
                "no variant produced at least "
                f"{MIN_TRADES_TO_RANK} validation trades on this history, so nothing "
                "was chosen and the test window was not used"
            )

        results.append(
            SeriesResult(
                symbol=symbol,
                timeframe=timeframe,
                coverage=entry,
                dataset_id=dataset_id,
                dataset_fingerprint=fingerprint,
                bars=len(series),
                train_bars=train_window[1] - train_window[0],
                validation_bars=validation_window[1] - validation_window[0],
                test_bars=test_window[1] - test_window[0],
                costs=cost.audit_view(),
                variants=tuple(measured),
                selected_variant_id=selected_id,
                selection_reason=selection_reason,
                walk_forward=walk,
                sensitivity=tuple(sensitivity),
                signal_fallbacks=fallbacks,
                signal_bars=len(series) * len(candidates),
                elapsed_seconds=clock() - series_started,
            )
        )

    if budget.cancelled():
        status = STATUS_CANCELLED
    elif budget.exhausted() or not_run:
        status = STATUS_TIMEOUT
    else:
        status = STATUS_COMPLETE
    notes.append(CLAIM_NOTE)
    notes.append(
        "Fills are the next bar's open on the side the trade pays: ask for a buy, bid "
        "for a sell, plus the assumed slippage. Spread is the bar's own quoted spread."
    )
    if results:
        notes.append(
            "The locked test window is spent once, on the variant chosen by validation. "
            "Its numbers are historical, not a forecast."
        )
    finished = now or _utc_now()
    return SweepReport(
        status=status,
        started_at=_iso(started),
        finished_at=_iso(finished),
        elapsed_seconds=budget.spent(),
        budget_seconds=budget.seconds,
        series=tuple(results),
        not_run=tuple(not_run),
        failures=tuple(failures),
        notes=tuple(notes),
        lot_size=lot_size,
        window_days=window_days,
    )


def _directions(signals: Sequence[Signal]) -> str:
    """Which sides a rule actually asked for on this history.

    Reported rather than assumed: a template that never asked for a short here did
    not take one, and a reader can check that claim against the same signals.
    """

    longs = any(signal == Signal.BUY for signal in signals)
    shorts = any(signal == Signal.SELL for signal in signals)
    if longs and shorts:
        return "LONG_AND_SHORT"
    if longs:
        return "LONG_ONLY"
    if shorts:
        return "SHORT_ONLY"
    return "NONE"


def _unavailable_reason(
    card: StrategyCard, timeframe: str, capabilities: Capabilities
) -> str:
    """Why a card cannot be measured here, or an empty string if it can."""

    availability = card.availability(capabilities)
    if not availability["evaluable"]:
        return str(availability["reason"] or "this template cannot be evaluated here")
    if not card.supports(timeframe):
        return (
            f"the template is declared for {', '.join(card.timeframes)} only, "
            f"not {timeframe}"
        )
    return ""


# -- ledger ----------------------------------------------------------------


def record_attempts(report: SweepReport, ledger: Any, *, note: str = "") -> list[str]:
    """Write every measured variant to the strategy ledger, pass or fail.

    A variant that was tried and came out badly stays in the record: the ledger is
    what stops a search from turning into a story about one lucky combination.  The
    status here is always ``ATTEMPTED`` -- whether an attempt is *eligible* is the
    predeclared screen's decision, not this module's.
    """

    from axiom_forex.strategies.library import ledger as ledger_module

    written: list[str] = []
    for result in report.series:
        for item in result.variants:
            if item.status == "UNAVAILABLE":
                status = ledger_module.UNAVAILABLE
            elif item.status in ("FAILED", "NOT_RUN"):
                status = ledger_module.UNAVAILABLE
            else:
                status = ledger_module.ATTEMPTED
            metrics = {
                "bars": result.bars,
                "train_bars": result.train_bars,
                "validation_bars": result.validation_bars,
                "test_bars": result.test_bars,
                "price_basis": result.coverage.price_basis,
                "coverage_fraction": round(result.coverage.coverage_fraction, 4),
                "lot_size": report.lot_size,
                "costs": dict(result.costs.get("labels") or {}),
                "costs_incomplete": list(result.costs.get("incomplete") or []),
                "claim": report.claim,
            }
            for name in SEGMENTS:
                stats = getattr(item, name)
                if stats is not None:
                    metrics[name] = stats.audit_view()
            if result.walk_forward is not None and item.variant_id == result.selected_variant_id:
                metrics["walk_forward"] = result.walk_forward.audit_view()
            if result.sensitivity and item.variant_id == result.selected_variant_id:
                metrics["sensitivity"] = [dict(row) for row in result.sensitivity]
            checks = [
                {
                    "check": "costs_complete",
                    "state": "UNKNOWN" if result.costs.get("incomplete") else "KNOWN",
                },
                {
                    "check": "selected_on_validation",
                    "state": "YES" if item.variant_id == result.selected_variant_id else "NO",
                },
                {"check": "note", "state": note or "one_year_terminal_history"},
            ]
            attempt = ledger.record(
                variant_id=item.variant_id,
                card_id=item.card_id,
                grid_version=GRID_VERSION,
                symbol=item.symbol,
                timeframe=item.timeframe,
                dataset_id=result.dataset_id,
                dataset_fingerprint=result.dataset_fingerprint,
                status=status,
                parameters=dict(item.params),
                metrics=metrics,
                reason=item.reason,
                checks=checks,
            )
            written.append(attempt.attempt_id)
    return written


__all__ = [
    "ASSUMED_ZERO",
    "CLAIM_NOTE",
    "CostModel",
    "CoverageEntry",
    "DEFAULT_BUDGET_SECONDS",
    "ESTIMATE",
    "MIN_TRADES_TO_RANK",
    "OBSERVED",
    "SEGMENTS",
    "SPLIT",
    "STATUS_CANCELLED",
    "STATUS_COMPLETE",
    "STATUS_TIMEOUT",
    "SegmentStats",
    "SeriesResult",
    "SweepCancelled",
    "SweepReport",
    "TIMEFRAME_SECONDS",
    "Trade",
    "UNKNOWN",
    "VariantResult",
    "WALK_FORWARD_FOLDS",
    "WalkForward",
    "record_attempts",
    "run_sweep",
    "segment_stats",
    "series_identity",
    "simulate",
]
