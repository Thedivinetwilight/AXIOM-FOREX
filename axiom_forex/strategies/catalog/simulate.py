"""Causal simulation of one catalog candidate over one series.

What a decision may see, and what it may not:

* entry and filter evidence is read at bar ``i``'s close;
* the fill happens at bar ``i + 1``'s **open** -- ask for a buy, bid for a sell,
  plus the stated slippage against the trade;
* stops, targets and trailing levels are then checked inside each later bar,
  using that bar's high and low;
* if a bar touches both the stop and the target, the **stop** is assumed to fill
  first, at the stop level.  That is the pessimistic reading, and it is the only
  defensible one without tick data.

Costs come from the shared research cost model: the spread each bar itself quoted
is already inside its bid, slippage is the stated assumption, commission and swap
are included only when they can be priced, and everything unpriceable is named in
the report rather than dropped.

Nothing here sizes a position (the lot is the operator's fixed size), touches an
account, or knows that an order API exists.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from axiom_forex.research.engine import CostModel, Trade

from .candidates import Candidate
from .indicators import IndicatorCache, Number
from .primitives import Context, HigherTimeframe, _exit_rsi_signal, _exit_ma_signal, direction_mask

#: Exit reasons this simulator can produce.
SIGNAL_EXIT = "SIGNAL_EXIT"
OPPOSING = "OPPOSING_ENTRY"
STOP = "STOP_LOSS"
TARGET = "TARGET"
TRAIL = "TRAILING_STOP"
TIME = "TIME_EXIT"
END = "FORCED"  # the engine's word for an exit forced by a window end
FILTERED_OUT = "FILTER_CLOSED"


@dataclass(frozen=True)
class Signals:
    """The causal event arrays for one candidate over one series.

    Plain lists, in bar order, one entry per bar.  They are read-only by
    convention: the simulator never writes into them.
    """

    bars: int
    longs: Sequence[bool]
    shorts: Sequence[bool]
    exit_events: Sequence[bool]
    allowed_long: Sequence[bool]
    allowed_short: Sequence[bool]
    atr: Sequence[Number]
    events: int

    def audit_view(self) -> dict[str, Any]:
        return {
            "bars": self.bars,
            "long_events": sum(self.longs),
            "short_events": sum(self.shorts),
            "exit_events": sum(self.exit_events),
            "bars_allowed": sum(self.allowed_long),
            "atr_ready_bars": sum(1 for value in self.atr if value is not None),
        }


def context_for(
    series: Any,
    cache: IndicatorCache,
    candidate: Candidate,
    *,
    coarse: HigherTimeframe | None = None,
) -> Context:
    """Attach a coarse view only when the candidate's filter needs one.

    ``coarse`` lets a caller hand in a bucket series it already built for this
    ratio; the buckets depend on the series and the ratio alone, never on the
    candidate, so sharing one is safe as well as cheaper.
    """

    if candidate.filter_kind.needs_coarse and coarse is None:
        coarse = HigherTimeframe.build(series, int(candidate.filter_params.get("ratio", 4)))
    return Context(series=series, cache=cache, coarse=coarse)


def signals_for(candidate: Candidate, context: Context) -> Signals:
    """Compute every candidate event array once, causally."""

    cache = context.cache
    bars = cache.bars
    entry = candidate.entry_kind
    longs, shorts = entry.compute(context, candidate.entry_params)
    allowed_long, allowed_short = direction_mask(
        candidate.filter_kind, context, candidate.filter_params
    )
    exit_block = candidate.exit_kind
    if exit_block.name == "ma_return":
        exit_events = _exit_ma_signal(context, candidate.exit_params)
    elif exit_block.name == "rsi_mid":
        exit_events = _exit_rsi_signal(context, candidate.exit_params)
    else:
        exit_events = [False] * bars
    policy = candidate.policy()
    ranges = cache.atr(int(policy.atr_window))
    return Signals(
        bars=bars,
        longs=longs,
        shorts=shorts,
        exit_events=exit_events,
        allowed_long=allowed_long,
        allowed_short=allowed_short,
        atr=ranges,
        events=sum(longs) + sum(shorts) + sum(exit_events),
    )


@dataclass
class _Open:
    direction: int
    index: int
    price: float
    mid: float
    when: datetime
    atr: float
    stop: float | None
    target: float | None
    trail: float | None
    best: float
    notional: float


def event_codes(candidate: Candidate, signals: Signals) -> list[int]:
    """One small integer per bar, so the inner loop tests a single value.

    bit 0 -- a long entry is allowed here
    bit 1 -- a short entry is allowed here
    bit 2 -- the candidate's own exit signal fired here
    """

    allowed_long = signals.allowed_long
    allowed_short = signals.allowed_short
    codes = [
        (1 if long_ok and allow_l else 0)
        | (2 if short_ok and allow_s else 0)
        | (4 if exit_now else 0)
        for long_ok, short_ok, exit_now, allow_l, allow_s in zip(
            signals.longs,
            signals.shorts,
            signals.exit_events,
            allowed_long,
            allowed_short,
        )
    ]
    if len(codes) < signals.bars:  # pragma: no cover - defensive
        codes.extend([0] * (signals.bars - len(codes)))
    return codes


def simulate_candidate(
    series: Any,
    signals: Signals,
    candidate: Candidate,
    *,
    cost: CostModel,
    lot: float,
    segment: str,
    start: int = 0,
    end: int | None = None,
) -> tuple[tuple[Trade, ...], int]:
    """Walk ``[start, end)`` and return the round turns, and the unfilled entries.

    The loop only visits bars that can matter: while flat it jumps to the next bar
    carrying an entry or exit event, and while a position is open it walks each bar
    because a stop or target can be touched anywhere.  That keeps a thousand
    candidates on a year of bars inside a desktop budget without changing a single
    decision: the semantics are the ones documented at the top of this module.
    """

    point = float(cost.point or getattr(series, "point", 0.0) or 0.0)
    contract = float(cost.contract_size or 0.0)
    pip_value = point * contract * float(lot)
    policy = candidate.policy()
    closes = list(getattr(series, "mid"))
    # The series' own bars are quoted on the bid side; ``mid`` is their midpoint.
    bids = list(getattr(series, "c"))
    highs = list(getattr(series, "mid_high"))
    lows = list(getattr(series, "mid_low"))
    spreads = list(getattr(series, "spread"))
    opens = list(getattr(series, "o"))
    stamps = list(getattr(series, "timestamps"))
    symbol = str(getattr(series, "symbol", ""))
    timeframe = str(getattr(series, "timeframe", ""))
    bars = min(len(closes), signals.bars)
    start = max(0, int(start))
    end = bars if end is None else min(bars, int(end))
    codes = event_codes(candidate, signals)
    event_indices = [index for index in range(start, end) if codes[index]] if end > start else []
    cursor = 0
    trades: list[Trade] = []
    unfilled = 0
    stop_atr = policy.stop_atr
    target_atr = policy.target_atr
    trail_atr = policy.trail_atr
    max_hold = policy.max_hold
    close_on_opposite = policy.close_on_opposite
    slip = cost.slippage_price
    commission = cost.commission_quote(lot)

    # -- position state, in locals for the inner loop ---------------------
    direction = 0
    entry_index = 0
    entry_price = 0.0
    entry_mid = 0.0
    entry_when = None
    entry_atr = 0.0
    stop_level: float | None = None
    target_level: float | None = None
    trail_level: float | None = None
    best_price = 0.0
    notional = 0.0

    def ask(index: int, price: float) -> float:
        return price + spreads[index]

    def record(index: int, price: float, reason: str) -> None:
        """Close the open position and book the round turn.

        Cost is the *modelled* half-spread plus slippage on each side, which is
        what this simulation actually charged: a level exit (a stop or a target)
        fills at a price fixed at entry time, so the difference from the exit bar's
        midpoint is path, not cost, and booking it as spread would produce a
        negative "cost" whenever a target filled inside a wide bar.  The gross
        figure is therefore the same trade at the mid with no slippage, and
        ``gross - net`` is exactly spread + slippage + commission + swap.
        """

        exit_mid = closes[index]
        nights = max(0, (stamps[index].date() - entry_when.date()).days)
        filled_quote = direction * (price - entry_price) * contract * lot
        entry_cost = spreads[entry_index] / 2.0 + slip
        exit_cost = spreads[index] / 2.0 + slip
        cost_price = entry_cost + exit_cost
        cost_quote = cost_price * contract * lot
        gross_quote = filled_quote + cost_quote
        swap = cost.swap_quote(direction, lot, nights)
        net_quote = filled_quote - (commission or 0.0) - (swap or 0.0)
        extra_pips = ((commission or 0.0) + (swap or 0.0)) / pip_value if pip_value else 0.0
        trades.append(
            Trade(
                variant_id=candidate.candidate_id,
                symbol=symbol,
                timeframe=timeframe,
                segment=segment,
                direction=direction,
                entry_at=entry_when.isoformat(),
                exit_at=stamps[index].isoformat(),
                entry_price=entry_price,
                exit_price=price,
                entry_mid=entry_mid,
                exit_mid=exit_mid,
                gross_quote=gross_quote,
                spread_slippage_quote=cost_quote,
                commission_quote=commission,
                swap_quote=swap,
                net_quote=net_quote,
                gross_pips=(direction * (exit_mid - entry_mid) / point) if point else 0.0,
                cost_pips=(cost_price / point) if point else 0.0,
                net_pips=(
                    (direction * (price - entry_price) / point) - extra_pips if point else 0.0
                ),
                nights=nights,
                exit_reason=reason,
                bars_held=max(0, index - entry_index),
                notional_quote=notional,
            )
        )

    def fill(direction_now: int, index: int, open_price: float) -> float:
        """Ask for a buy, bid for a sell, both with slippage against the trade."""

        if direction_now > 0:
            return open_price + spreads[index] + slip
        return open_price - slip

    def open_into(wanted: int, fill_index: int) -> None:
        """Open at ``fill_index``'s open, with the ATR bracket fixed right now."""

        nonlocal direction, entry_price, entry_mid, entry_index, entry_when
        nonlocal entry_atr, stop_level, target_level, trail_level, best_price, notional
        entry_price = fill(wanted, fill_index, opens[fill_index])
        entry_mid = closes[fill_index]
        entry_index = fill_index
        entry_when = stamps[fill_index]
        entry_atr = float(signals.atr[fill_index] or 0.0)
        direction = wanted
        stop_level = (
            entry_price - wanted * stop_atr * entry_atr if (stop_atr and entry_atr) else None
        )
        target_level = (
            entry_price + wanted * target_atr * entry_atr
            if (target_atr and entry_atr)
            else None
        )
        trail_level = entry_price if (trail_atr and entry_atr) else None
        best_price = entry_price
        notional = abs(entry_mid * contract * lot)

    index = start
    while index < end:
        if direction == 0:
            # Flat: jump straight to the next bar that can act.  The position may
            # have closed earlier on *this* bar, whose own event still counts.
            cursor = bisect.bisect_left(event_indices, index)
            if cursor >= len(event_indices):
                break
            index = event_indices[cursor]
            code = codes[index]
            if not (code & 1 or code & 2):
                index += 1
                continue
            if index + 1 >= end:
                unfilled += 1
                index += 1
                continue
            open_into(1 if code & 1 else -1, index + 1)
            index += 1
            continue

        # -- an open position: walk bars until something closes it -------
        code = codes[index]
        bar_high = highs[index]
        bar_low = lows[index]
        if trail_level is not None and trail_atr:
            if direction > 0:
                if bar_high > best_price:
                    best_price = bar_high
                candidate_level = best_price - trail_atr * entry_atr
                if candidate_level > trail_level:
                    trail_level = candidate_level
            else:
                if bar_low < best_price:
                    best_price = bar_low
                candidate_level = best_price + trail_atr * entry_atr
                if candidate_level < trail_level:
                    trail_level = candidate_level
        hit_stop = False
        if stop_level is not None:
            hit_stop = bar_low <= stop_level if direction > 0 else bar_high >= stop_level
        hit_target = False
        if target_level is not None:
            hit_target = bar_high >= target_level if direction > 0 else bar_low <= target_level
        hit_trail = False
        if trail_level is not None and index > entry_index:
            hit_trail = bar_low <= trail_level if direction > 0 else bar_high >= trail_level
        if hit_stop or hit_trail:
            # Pessimistic: a stop that is touched fills at the level, not better,
            # and it wins over a target touched in the same bar.
            level = stop_level if hit_stop else trail_level
            price = float(level) - direction * slip
            record(index, price, STOP if hit_stop else TRAIL)
            direction = 0
            continue
        if hit_target and target_level is not None:
            record(index, float(target_level) - direction * slip, TARGET)
            direction = 0
            continue
        if max_hold and index - entry_index >= max_hold:
            price = fill(-direction, index, opens[index])
            record(index, price, TIME)
            direction = 0
            continue
        if code & 4:
            if index + 1 >= end:
                direction = 0
                break
            price = fill(-direction, index + 1, opens[index + 1])
            record(index + 1, price, SIGNAL_EXIT)
            direction = 0
            if code & (1 | 2) and not close_on_opposite:
                # The same event bar can also be an entry: open the reverse now.
                open_into(1 if code & 1 else -1, index + 1)
            index += 1
            continue
        if code & (1 | 2):
            wanted = 1 if code & 1 else -1
            if wanted != direction and index + 1 < end:
                price = fill(-direction, index + 1, opens[index + 1])
                record(index + 1, price, OPPOSING)
                direction = 0
                if not close_on_opposite:
                    open_into(wanted, index + 1)
        index += 1

    if direction != 0 and end > start:
        last = end - 1
        # A forced exit at the last bar pays the bid to sell or the ask to buy.
        price = bids[last] - slip if direction > 0 else bids[last] + spreads[last] + slip
        record(last, price, END)
    return tuple(trades), unfilled


__all__ = [
    "END",
    "event_codes",
    "FILTERED_OUT",
    "OPPOSING",
    "SIGNAL_EXIT",
    "STOP",
    "Signals",
    "TARGET",
    "TIME",
    "TRAIL",
    "context_for",
    "signals_for",
    "simulate_candidate",
]
