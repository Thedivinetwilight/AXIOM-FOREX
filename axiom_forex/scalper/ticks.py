"""AX-TICK CORE: one reliable stream of quotes for the decision loop.

What this module is responsible for, and nothing else:

* **ingestion** of a bid and an ask with both a broker stamp and our receive stamp;
* **identity** — every accepted tick has a deterministic id and a monotonic local
  sequence number, and a repeat of content already seen in the window is refused;
* **ordering** — a tick stamped before the last accepted one is refused and
  counted, never quietly sorted into a decision.  A large backward jump is treated
  as a *clock reset* (counted separately) and followed by a resynchronisation
  window in which the desk may not trade;
* **quality** — staleness, crossed or non-positive quotes, spread width and spread
  expansion, gaps, tick rate, and the age of the last accepted quote;
* **latency** — measured, labelled, stored; never called exchange latency;
* **replay** — a historical tick file can be fed through the same core.

It makes no trading decision and holds no order path.

A note on time.  ``source_time`` is the broker's stamp and ``received_at`` is this
machine's clock.  ``received_at - source_time`` therefore mixes transport delay
with whatever clock skew the broker's server label carries.  It is reported as
``broker_clock_basis`` and is not evidence of exchange latency.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Deque, Iterable, Iterator, Mapping, Sequence

from .config import InstrumentSpec

UTC = timezone.utc

#: Sources.  The string reaches the journal, so it says where the tick came from.
SRC_POLL = "MT5_POLL"
SRC_HISTORY = "MT5_TICK_HISTORY"
SRC_REPLAY = "HISTORICAL_TICK_REPLAY"
SRC_SAMPLE = "SAMPLE_DATA"

#: Rejection reasons.
REJECT_UNKNOWN_SYMBOL = "unknown_symbol"
REJECT_INVALID_PRICE = "invalid_price"
REJECT_CROSSED = "crossed_quote"
REJECT_DUPLICATE = "duplicate_tick"
REJECT_OUT_OF_ORDER = "out_of_order_tick"
REJECT_STALE = "stale_tick"
REJECT_FUTURE = "tick_from_the_future"

#: Quality events.
EVENT_GAP = "tick_gap"
EVENT_SPREAD_EXPANSION = "spread_expansion"
EVENT_CLOCK_RESET = "clock_reset"
EVENT_RESYNCED = "resynced"
EVENT_STALE = "quote_stale"
EVENT_RATE_DROP = "tick_rate_drop"
EVENT_RATE_RECOVERED = "tick_rate_recovered"

#: How many quality events one core keeps.  A bound on memory, not on the journal.
EVENT_HISTORY = 512


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _dec(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _ms(delta: timedelta) -> float:
    return delta.total_seconds() * 1000.0


@dataclass(frozen=True)
class Tick:
    """One accepted quote.  Immutable: it is evidence, not a working row."""

    symbol: str
    bid: Decimal
    ask: Decimal
    source_time: datetime
    received_at: datetime
    sequence: int
    source: str
    ingest_latency_ms: float
    volume: Decimal | None = None

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def spread_points(self, point: Decimal) -> Decimal:
        return self.spread / point if point > 0 else Decimal(0)

    @property
    def tick_id(self) -> str:
        """Deterministic identity: the content, not the arrival order."""

        raw = f"{self.symbol}|{self.source_time.isoformat()}|{self.bid}|{self.ask}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def clock_basis(self) -> str:
        return "broker_clock_basis"

    def audit_view(self) -> dict[str, Any]:
        return {
            "tick_id": self.tick_id,
            "symbol": self.symbol,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "source_time": self.source_time.isoformat(),
            "received_at": self.received_at.isoformat(),
            "sequence": self.sequence,
            "source": self.source,
            "ingest_latency_ms": round(self.ingest_latency_ms, 3),
            "clock_basis": self.clock_basis,
        }


@dataclass(frozen=True)
class QualityEvent:
    kind: str
    symbol: str
    at: datetime
    detail: str
    severity: str = "WARNING"

    def audit_view(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "symbol": self.symbol,
            "at": self.at.isoformat(),
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    tick: Tick | None
    reason: str = ""
    detail: str = ""
    events: tuple[QualityEvent, ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "tick": None if self.tick is None else self.tick.audit_view(),
            "reason": self.reason,
            "detail": self.detail,
            "events": [event.audit_view() for event in self.events],
        }


@dataclass
class _SymbolState:
    last: Tick | None = None
    seen: Deque[str] = field(default_factory=deque)
    seen_set: set[str] = field(default_factory=set)
    window: Deque[Tick] = field(default_factory=deque)
    counters: dict[str, int] = field(default_factory=dict)
    spreads: Deque[Decimal] = field(default_factory=deque)
    resync_left: int = 0
    last_gap_ms: float = 0.0

    def count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1


class TickCore:
    """Validation, sequencing and monitoring for one or more symbols."""

    def __init__(
        self,
        *,
        instruments: Sequence[InstrumentSpec],
        max_quote_age_ms: int,
        max_spread_points: Decimal,
        gap_threshold_ms: int,
        spread_expansion_multiple: Decimal = Decimal("2.5"),
        tick_window: int = 240,
        min_window_ticks: int = 10,
        duplicate_window: int = 2048,
        clock_reset_threshold_ms: int = 60_000,
        resync_ticks: int = 5,
        clock: Callable[[], datetime] | None = None,
        event_history: int = EVENT_HISTORY,
    ) -> None:
        self._specs = {spec.symbol: spec for spec in instruments}
        self._max_age_ms = int(max_quote_age_ms)
        self._max_spread_points = Decimal(max_spread_points)
        self._gap_ms = int(gap_threshold_ms)
        self._expansion = Decimal(spread_expansion_multiple)
        self._duplicate_window = max(16, int(duplicate_window))
        self._min_window = max(1, int(min_window_ticks))
        self._clock_reset_ms = int(clock_reset_threshold_ms)
        self._resync_ticks = max(1, int(resync_ticks))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state = {
            symbol: _SymbolState(window=deque(maxlen=int(tick_window)))
            for symbol in self._specs
        }
        self._events: Deque[QualityEvent] = deque(maxlen=int(event_history))
        self._latencies: Deque[float] = deque(maxlen=1024)
        self._sequence = 0

    # -- ingestion -------------------------------------------------------

    def ingest(
        self,
        *,
        symbol: str,
        bid: Any,
        ask: Any,
        source_time: datetime,
        received_at: datetime | None = None,
        source: str = SRC_POLL,
        volume: Any = None,
    ) -> IngestResult:
        state = self._state.get(str(symbol))
        if state is None:
            return IngestResult(
                False, None, REJECT_UNKNOWN_SYMBOL, f"{symbol} was never declared"
            )
        spec = self._specs[str(symbol)]
        bid_dec, ask_dec = _dec(bid), _dec(ask)
        if bid_dec is None or ask_dec is None or bid_dec <= 0 or ask_dec <= 0:
            state.count(REJECT_INVALID_PRICE)
            return IngestResult(False, None, REJECT_INVALID_PRICE, "bid or ask is not positive")
        if ask_dec < bid_dec:
            state.count(REJECT_CROSSED)
            return IngestResult(False, None, REJECT_CROSSED, f"bid {bid_dec} above ask {ask_dec}")

        source_time = _utc(source_time)
        received_at = _utc(received_at or self._clock())
        latency_ms = _ms(received_at - source_time)

        candidate = Tick(
            symbol=str(symbol),
            bid=bid_dec,
            ask=ask_dec,
            source_time=source_time,
            received_at=received_at,
            sequence=self._sequence + 1,
            source=str(source),
            ingest_latency_ms=latency_ms,
            volume=_dec(volume),
        )

        # 1. content identity, within the dedup window
        if candidate.tick_id in state.seen_set:
            state.count(REJECT_DUPLICATE)
            return IngestResult(
                False,
                None,
                REJECT_DUPLICATE,
                f"content already accepted (tick {candidate.tick_id})",
            )

        # 2. ordering
        events: list[QualityEvent] = []
        if state.last is not None and source_time < state.last.source_time:
            backwards_ms = _ms(state.last.source_time - source_time)
            if backwards_ms > self._clock_reset_ms:
                state.count(EVENT_CLOCK_RESET)
                state.resync_left = self._resync_ticks
                events.append(
                    QualityEvent(
                        EVENT_CLOCK_RESET,
                        str(symbol),
                        received_at,
                        f"source time moved back {backwards_ms:.0f} ms; "
                        f"re-entering service after {self._resync_ticks} clean tick(s)",
                        "ERROR",
                    )
                )
                # The reset is recorded before the tick is judged: a reset that is then
                # refused as stale is still a reset, and the desk must not keep trading
                # on an ordering the core already distrusts.
                self._record(events[-1])
                # Chronology is broken, so the ordering baseline and the price window
                # are both discarded: comparing a post-reset stamp with a pre-reset one
                # would reject every tick until the old clock caught up.
                state.last = None
                state.window.clear()
                state.spreads.clear()
            else:
                state.count(REJECT_OUT_OF_ORDER)
                return IngestResult(
                    False,
                    None,
                    REJECT_OUT_OF_ORDER,
                    f"stamped {backwards_ms:.0f} ms before the last accepted tick",
                )

        # 3. our own clock: a tick stamped ahead of us cannot be acted on either
        if _ms(source_time - received_at) > self._max_age_ms:
            state.count(REJECT_FUTURE)
            return IngestResult(
                False,
                None,
                REJECT_FUTURE,
                "the source stamp is ahead of this machine's clock",
                events=tuple(events),
            )

        # 4. staleness: too old to act on, refused rather than used late
        if latency_ms > self._max_age_ms:
            state.count(REJECT_STALE)
            state.count(EVENT_STALE)
            self._record(
                QualityEvent(
                    EVENT_STALE,
                    str(symbol),
                    received_at,
                    f"quote is {latency_ms:.0f} ms old (limit {self._max_age_ms} ms)",
                )
            )
            return IngestResult(
                False,
                None,
                REJECT_STALE,
                f"{latency_ms:.0f} ms old, limit is {self._max_age_ms} ms",
                events=tuple(events),
            )

        # accepted
        previous = state.last
        self._sequence = candidate.sequence
        if previous is not None:
            gap_ms = _ms(source_time - previous.source_time)
            if gap_ms > self._gap_ms:
                state.last_gap_ms = gap_ms
                state.count(EVENT_GAP)
                events.append(
                    QualityEvent(
                        EVENT_GAP,
                        str(symbol),
                        received_at,
                        f"{gap_ms:.0f} ms with no tick (threshold {self._gap_ms} ms)",
                    )
                )
        state.last = candidate
        state.window.append(candidate)
        self._remember(state, candidate.tick_id)

        spread_points = candidate.spread_points(spec.point)
        baseline = self._spread_baseline(state)
        state.spreads.append(spread_points)
        if len(state.spreads) > 240:
            state.spreads.popleft()
        if spread_points > self._max_spread_points or (
            baseline is not None and spread_points > baseline * self._expansion
        ):
            state.count(EVENT_SPREAD_EXPANSION)
            events.append(
                QualityEvent(
                    EVENT_SPREAD_EXPANSION,
                    str(symbol),
                    received_at,
                    f"spread {spread_points:.1f} points"
                    + (
                        f" (baseline {baseline:.1f}, limit {self._max_spread_points})"
                        if baseline is not None
                        else f" (limit {self._max_spread_points})"
                    ),
                )
            )

        if state.resync_left > 0:
            state.resync_left -= 1
            if state.resync_left == 0:
                events.append(
                    QualityEvent(
                        EVENT_RESYNCED,
                        str(symbol),
                        received_at,
                        "resynchronised after a clock reset",
                        "INFO",
                    )
                )

        self._latencies.append(latency_ms)
        for event in events:
            self._record(event)
        return IngestResult(True, candidate, events=tuple(events))

    def _remember(self, state: _SymbolState, tick_id: str) -> None:
        state.seen.append(tick_id)
        state.seen_set.add(tick_id)
        while len(state.seen) > self._duplicate_window:
            old = state.seen.popleft()
            state.seen_set.discard(old)

    def _spread_baseline(self, state: _SymbolState) -> Decimal | None:
        if len(state.spreads) < 10:
            return None
        ordered = sorted(state.spreads)
        return ordered[len(ordered) // 2]

    def _record(self, event: QualityEvent) -> None:
        self._events.append(event)

    # -- reads -----------------------------------------------------------

    def last(self, symbol: str) -> Tick | None:
        state = self._state.get(str(symbol))
        return None if state is None else state.last

    def window(self, symbol: str) -> tuple[Tick, ...]:
        state = self._state.get(str(symbol))
        return () if state is None else tuple(state.window)

    def sequence(self) -> int:
        return self._sequence

    def events(self, *, symbol: str | None = None, limit: int = 50) -> list[QualityEvent]:
        items = [
            event for event in self._events if symbol is None or event.symbol == symbol
        ]
        return items[-limit:]

    def age_ms(self, symbol: str, *, now: datetime | None = None) -> float | None:
        tick = self.last(symbol)
        if tick is None:
            return None
        return _ms(_utc(now or self._clock()) - tick.source_time)

    def spread_points(self, symbol: str) -> Decimal | None:
        tick = self.last(symbol)
        spec = self._specs.get(str(symbol))
        if tick is None or spec is None:
            return None
        return tick.spread_points(spec.point)

    def latency(self) -> dict[str, Any]:
        """Measured ingest delay.  Observational, on this machine, on these stamps."""

        values = sorted(self._latencies)
        if not values:
            return {
                "samples": 0,
                "basis": "broker_clock_basis",
                "note": "no tick has been accepted yet",
            }
        def pct(fraction: float) -> float:
            index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
            return values[index]

        return {
            "samples": len(values),
            "mean_ms": round(sum(values) / len(values), 3),
            "p50_ms": round(pct(0.50), 3),
            "p95_ms": round(pct(0.95), 3),
            "max_ms": round(values[-1], 3),
            "basis": "broker_clock_basis",
            "note": (
                "received_at minus the broker's own stamp: transport delay plus any "
                "clock skew on that server label. Not exchange latency."
            ),
        }

    def quality(self, symbol: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Everything a decision needs to know about whether this feed may be used."""

        state = self._state.get(str(symbol))
        spec = self._specs.get(str(symbol))
        if state is None or spec is None:
            return {"symbol": symbol, "usable": False, "reasons": ["unknown_symbol"]}
        tick = state.last
        age = self.age_ms(symbol, now=now)
        spread = None if tick is None else tick.spread_points(spec.point)
        reasons: list[str] = []
        if tick is None:
            reasons.append("no_tick")
        else:
            if age is None or age > self._max_age_ms:
                reasons.append("stale_quote")
            if spread is not None and spread > self._max_spread_points:
                reasons.append("spread_too_wide")
        if len(state.window) < self._min_window:
            reasons.append("insufficient_window")
        if state.resync_left > 0:
            reasons.append("resynchronising")
        counters = dict(state.counters)
        return {
            "symbol": symbol,
            "usable": not reasons,
            "reasons": reasons,
            "ticks": len(state.window),
            "last_age_ms": None if age is None else round(age, 3),
            "last_spread_points": None if spread is None else str(spread),
            "max_spread_points": str(self._max_spread_points),
            "counters": counters,
            "last_gap_ms": round(state.last_gap_ms, 3),
            "sequence": self._sequence,
            "latency": self.latency(),
            "note": (
                "One broker's quote stream, not a market-wide order book. "
                "Absence of a tick here says nothing about the rest of the market."
            ),
        }

    @property
    def minimum_window(self) -> int:
        return self._min_window

    def resyncing(self, symbol: str | None = None) -> bool:
        """True while a clock reset is still being absorbed.

        Trading stays off during this window: the sequence numbers after a reset
        cannot be compared with the ones before it, so a decision taken then would be
        taken on an ordering the core has already declared unreliable.
        """

        if symbol is not None:
            state = self._state.get(str(symbol))
            return bool(state and state.resync_left > 0)
        return any(state.resync_left > 0 for state in self._state.values())

    def describe(self) -> dict[str, Any]:
        return {
            "symbols": sorted(self._specs),
            "max_quote_age_ms": self._max_age_ms,
            "max_spread_points": str(self._max_spread_points),
            "gap_threshold_ms": self._gap_ms,
            "sequence": self._sequence,
            "events": [event.audit_view() for event in self.events(limit=20)],
            "latency": self.latency(),
        }


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayTick:
    symbol: str
    bid: Decimal
    ask: Decimal
    at: datetime
    volume: Decimal | None = None

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


def read_tick_file(path: str | Path) -> list[ReplayTick]:
    """Read a JSONL tick file.  Strict: a malformed line is an error, not a skip.

    Silently dropping a line from a historical file would change the series a
    result was measured on, so the file has to be right.
    """

    ticks: list[ReplayTick] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"line {number}: not JSON ({exc})") from None
        if not isinstance(payload, Mapping):
            raise ValueError(f"line {number}: not an object")
        at = payload.get("at") or payload.get("time")
        if not isinstance(at, str):
            raise ValueError(f"line {number}: no timestamp")
        try:
            stamp = _utc(datetime.fromisoformat(at.replace("Z", "+00:00")))
        except ValueError as exc:
            raise ValueError(f"line {number}: bad timestamp ({exc})") from None
        bid, ask = _dec(payload.get("bid")), _dec(payload.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError(f"line {number}: bid/ask is not a quotable pair")
        ticks.append(
            ReplayTick(
                symbol=str(payload.get("symbol") or ""),
                bid=bid,
                ask=ask,
                at=stamp,
                volume=_dec(payload.get("volume")),
            )
        )
    ticks.sort(key=lambda item: (item.at, item.symbol))
    return ticks


class HistoricalTickFeed:
    """Replay a stored tick sequence at the wall-clock pace it was recorded.

    ``latency_ms`` is added to each receive stamp, so a replay can exercise the same
    staleness path as a live feed.

    By default the receive stamp is derived from the tick's **own** stamp plus the
    declared latency.  Replaying a file recorded last month against today's clock
    would make every tick months old and every one of them stale, which measures
    nothing.  Passing ``clock=`` restores wall-clock behaviour, and the report says
    which basis was used.  A replay is always labelled with its own source and never
    blended with live ticks.
    """

    data_class = SRC_REPLAY

    def __init__(
        self,
        ticks: Iterable[ReplayTick],
        *,
        latency_ms: float = 0.0,
        speed: float = 1000.0,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._ticks = list(ticks)
        self._latency_ms = float(latency_ms)
        self._speed = float(speed) if speed > 0 else 1000.0
        self._wall_clock = clock is not None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep

    def __len__(self) -> int:
        return len(self._ticks)

    @property
    def received_at_basis(self) -> str:
        return (
            "process_clock" if self._wall_clock else "recorded_tick_stamp_plus_declared_latency"
        )

    def iter_ticks(self) -> Iterator[tuple[ReplayTick, datetime]]:
        previous: datetime | None = None
        for tick in self._ticks:
            if previous is not None:
                wait_ms = _ms(tick.at - previous)
                if wait_ms > 0:
                    self._sleep(wait_ms / 1000.0 / self._speed)
            previous = tick.at
            anchor = self._clock() if self._wall_clock else tick.at
            received = _utc(anchor + timedelta(milliseconds=self._latency_ms))
            yield tick, received

    def feed_into(self, core: TickCore) -> dict[str, Any]:
        """Push every tick through a core and report what the core made of it."""

        accepted = 0
        refused: dict[str, int] = {}
        for tick, received in self.iter_ticks():
            result = core.ingest(
                symbol=tick.symbol,
                bid=tick.bid,
                ask=tick.ask,
                source_time=tick.at,
                received_at=received,
                source=self.data_class,
                volume=tick.volume,
            )
            if result.accepted:
                accepted += 1
            else:
                refused[result.reason] = refused.get(result.reason, 0) + 1
        return {
            "ticks": len(self._ticks),
            "accepted": accepted,
            "refused": refused,
            "data_class": self.data_class,
            "received_at_basis": self.received_at_basis,
            "note": (
                "A replay of stored ticks. Its receive stamps are not evidence of live "
                "latency, and the price path is whatever the file holds."
            ),
        }


# ---------------------------------------------------------------------------
# benchmark
# ---------------------------------------------------------------------------


def benchmark_ingest(
    ticks: Sequence[ReplayTick],
    *,
    instruments: Sequence[InstrumentSpec],
    repeat: int = 3,
) -> dict[str, Any]:
    """Measure how fast this core actually takes ticks on this machine.

    The number is a local processing rate, not an evidence of exchange latency, and
    it is reported that way.  It answers one question honestly: can a Python loop
    keep up with the tick rate this broker is delivering?
    """

    if not ticks:
        return {"ticks": 0, "error": "no ticks to measure"}
    rates: list[float] = []
    per_tick_us: list[float] = []
    for _ in range(max(1, int(repeat))):
        core = TickCore(
            instruments=instruments,
            max_quote_age_ms=60_000,
            max_spread_points=Decimal("1000"),
            gap_threshold_ms=60_000,
            tick_window=max(16, min(240, len(ticks))),
        )
        started = time.perf_counter()
        accepted = 0
        for tick in ticks:
            result = core.ingest(
                symbol=tick.symbol,
                bid=tick.bid,
                ask=tick.ask,
                source_time=tick.at,
                received_at=tick.at,
                source=SRC_REPLAY,
            )
            if result.accepted:
                accepted += 1
        elapsed = time.perf_counter() - started
        if elapsed > 0:
            rates.append(accepted / elapsed)
            per_tick_us.append(elapsed / max(1, accepted) * 1_000_000)
    rates.sort()
    per_tick_us.sort()
    return {
        "ticks": len(ticks),
        "repeats": len(rates),
        "ticks_per_second_median": round(rates[len(rates) // 2], 1) if rates else 0.0,
        "ticks_per_second_min": round(rates[0], 1) if rates else 0.0,
        "microseconds_per_tick_median": round(per_tick_us[len(per_tick_us) // 2], 2)
        if per_tick_us
        else 0.0,
        "basis": "this machine, this process, standard-library Decimal arithmetic",
        "note": (
            "A local processing rate. It says whether a poll-and-process loop can keep "
            "up with the broker's tick rate; it says nothing about exchange latency or "
            "about the terminal's own delivery delay."
        ),
    }


def synthetic_ticks(
    *,
    symbol: str = "EURUSD.pro",
    count: int = 200,
    start: datetime | None = None,
    step_ms: int = 250,
    mid: str = "1.10000",
    spread_points: str = "8",
    point: str = "0.00001",
    drift_points: float = 0.0,
    noise_points: float = 3.0,
    phase: float = 0.0,
) -> list[ReplayTick]:
    """Deterministic synthetic ticks for tests and offline demonstrations.

    Not market data: the label on every use is ``SAMPLE_DATA``.  The series is a
    sine plus a linear drift, so a test can assert a direction without depending on
    a random seed.
    """

    base = _dec(mid) or Decimal("1.1")
    point_dec = Decimal(point)
    spread = Decimal(spread_points) * point_dec
    start = _utc(start or datetime(2026, 3, 2, 9, 0, tzinfo=UTC))
    values: list[ReplayTick] = []
    for index in range(int(count)):
        offset = math.sin(phase + index / 7.0) * noise_points
        move = Decimal(str(round(offset + drift_points * index, 8))) * point_dec
        price = base + move
        half = spread / Decimal(2)
        values.append(
            ReplayTick(
                symbol=symbol,
                bid=price - half,
                ask=price + half,
                at=start + timedelta(milliseconds=step_ms * index),
            )
        )
    return values


__all__ = [
    "EVENT_CLOCK_RESET",
    "EVENT_GAP",
    "EVENT_RATE_DROP",
    "EVENT_RATE_RECOVERED",
    "EVENT_RESYNCED",
    "EVENT_SPREAD_EXPANSION",
    "EVENT_STALE",
    "HistoricalTickFeed",
    "IngestResult",
    "QualityEvent",
    "REJECT_CROSSED",
    "REJECT_DUPLICATE",
    "REJECT_FUTURE",
    "REJECT_INVALID_PRICE",
    "REJECT_OUT_OF_ORDER",
    "REJECT_STALE",
    "REJECT_UNKNOWN_SYMBOL",
    "ReplayTick",
    "SRC_HISTORY",
    "SRC_POLL",
    "SRC_REPLAY",
    "SRC_SAMPLE",
    "Tick",
    "TickCore",
    "benchmark_ingest",
    "read_tick_file",
    "synthetic_ticks",
]
