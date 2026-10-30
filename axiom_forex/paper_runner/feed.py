"""Market-data feeds for the paper strategy runner.

A feed answers exactly one question per call: *what is the newest completed bar,
and what price may be used to act on it?*  Nothing here can send, arm, or
connect anything, and nothing here decides whether to trade.

Two feeds, and their data class is never blended:

``TerminalBarFeed`` (``TERMINAL_TICKS``)
    Polls the *data* surface of the connected MetaTrader 5 terminal -- the same
    ``TerminalMarketData`` object paper trading already prices from -- and
    aggregates its bid/ask ticks into fixed-interval bars.  A bar is emitted only
    when a tick arrives at or after the bar's end, so its prices are complete and
    nothing in it belongs to the future.  The tick that closes the window is the
    price any order may then be filled at, because it is stamped after the
    decision bar closed.

``ReplayBarFeed`` (``HISTORICAL_REPLAY``)
    Walks a stored bar sequence offline.  The decision bar is bar *i* and the
    executable price is bar *i+1*'s open (ask for a buy, bid for a sell), which is
    exactly the convention the backtest engine uses.  This is the only feed that
    works without a terminal, and it is always labelled as replay.

Failure modes are explicit events, never silent gaps: ``STALE`` (no tick for
longer than the policy allows), ``GAP`` (windows were skipped, so a bar would
have spanned missing time), ``DISCONNECTED``, ``INVALID``, and ``END``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from app.data.models import ForexQuote, ForexQuoteBar, QuoteFreshness, QuoteSideBar

#: Feed kinds.  These strings reach the UI, so they are deliberately blunt.
KIND_TERMINAL = "TERMINAL_TICKS"
KIND_REPLAY = "HISTORICAL_REPLAY"

#: Event kinds.
TICK = "TICK"
BAR = "BAR"
GAP = "GAP"
STALE = "STALE"
DISCONNECTED = "DISCONNECTED"
INVALID = "INVALID"
END = "END"
ERROR = "ERROR"

#: Reasons that stop the runner outright (the loop cannot continue safely).
FATAL_EVENTS = frozenset({DISCONNECTED, END, ERROR})

BAR_SOURCE = "AXIOM_PAPER_RUNNER_TICK_AGGREGATE"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FeedPolicy:
    """How fresh a price must be, and how a bar is assembled.

    ``max_quote_age_seconds`` is the age of the newest tick at the moment it is
    used.  ``interval_seconds`` is the bar interval.  ``max_gap_windows`` is how
    many skipped windows are tolerated before the next bar is treated as spanning
    a hole -- above it the bar is refused rather than traded through.
    """

    interval_seconds: int = 60
    max_quote_age_seconds: Decimal = Decimal("5")
    max_future_skew_seconds: Decimal = Decimal("2")
    max_gap_windows: int = 0
    timeframe: str = "1m"

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not isinstance(self.interval_seconds, int) or self.interval_seconds <= 0:
            errors.append("invalid_interval_seconds")
        if self.max_quote_age_seconds <= 0:
            errors.append("invalid_max_quote_age")
        if self.max_gap_windows < 0:
            errors.append("invalid_max_gap_windows")
        if not str(self.timeframe).strip():
            errors.append("missing_timeframe")
        return tuple(errors)

    def freshness(self) -> QuoteFreshness:
        return QuoteFreshness(
            policy_id=f"paper-runner-{self.interval_seconds}s",
            version="runner-v1",
            max_age_seconds=Decimal(self.max_quote_age_seconds),
            max_future_skew_seconds=Decimal(self.max_future_skew_seconds),
            max_feed_latency_seconds=Decimal(self.max_quote_age_seconds),
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "interval_seconds": self.interval_seconds,
            "timeframe": self.timeframe,
            "max_quote_age_seconds": str(self.max_quote_age_seconds),
            "max_future_skew_seconds": str(self.max_future_skew_seconds),
            "max_gap_windows": self.max_gap_windows,
        }


@dataclass(frozen=True)
class FeedEvent:
    """One poll result."""

    kind: str
    at: datetime | None = None
    quote: ForexQuote | None = None
    bar: ForexQuoteBar | None = None
    reason: str = ""
    detail: str = ""
    skipped_windows: int = 0

    @property
    def has_bar(self) -> bool:
        return self.kind == BAR and self.bar is not None

    def audit_view(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat() if self.at else "",
            "reason": self.reason,
            "detail": self.detail,
            "skipped_windows": self.skipped_windows,
            "bar_end": self.bar.end_at.isoformat() if self.bar is not None else "",
            "quote_at": (
                self.quote.timestamp.isoformat() if self.quote is not None else ""
            ),
        }


@runtime_checkable
class BarFeed(Protocol):
    """What the runner needs from a feed.  Read-only by construction."""

    kind: str
    data_class: str
    policy: FeedPolicy

    def poll(self) -> FeedEvent: ...

    def describe(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# live terminal ticks
# ---------------------------------------------------------------------------


def _bar_from_quotes(
    *,
    pair: str,
    window_start: datetime,
    interval_seconds: int,
    quotes: Sequence[ForexQuote],
    timeframe: str,
) -> ForexQuoteBar:
    """Aggregate quotes into a two-sided bar.  Raises ValueError if impossible."""

    if not quotes:
        raise ValueError("empty_window")
    bids = [quote.prices()[0] for quote in quotes]
    asks = [quote.prices()[1] for quote in quotes]
    last_quote_at = max(quote.timestamp_utc() for quote in quotes)
    bar = ForexQuoteBar(
        pair=pair,
        timeframe=timeframe,
        timestamp=window_start,
        duration_seconds=interval_seconds,
        bid=QuoteSideBar(
            open=bids[0], high=max(bids), low=min(bids), close=bids[-1]
        ),
        ask=QuoteSideBar(
            open=asks[0], high=max(asks), low=min(asks), close=asks[-1]
        ),
        source=BAR_SOURCE,
        quote_count=len(quotes),
        last_quote_at=last_quote_at,
        complete=True,
    )
    return bar


class TerminalBarFeed:
    """Completed bars aggregated from a terminal's own bid/ask ticks.

    ``market`` is a ``TerminalMarketData`` (data-only: ``quote`` and
    ``resolve_symbol``).  It is polled; nothing is pushed, nothing is retried,
    and a terminal that stops answering produces ``DISCONNECTED`` rather than a
    stale bar.
    """

    kind = KIND_TERMINAL
    data_class = "LIVE_BROKER_TICK"

    def __init__(
        self,
        *,
        market: Any,
        spec: Any,
        policy: FeedPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.market = market
        self.spec = spec
        self.policy = policy or FeedPolicy()
        self.clock = clock or _utc_now
        self._window_start: datetime | None = None
        self._quotes: list[ForexQuote] = []
        self._last_quote: ForexQuote | None = None
        self._skipped = 0
        self._symbol = ""
        self.error = ""

    # -- internals -------------------------------------------------------

    def _window_index(self, at: datetime) -> int:
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return int((at - epoch).total_seconds() // self.policy.interval_seconds)

    def _window_at(self, index: int) -> datetime:
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return epoch + timedelta(seconds=index * self.policy.interval_seconds)

    def _resolve(self) -> None:
        if self._symbol:
            return
        resolve = getattr(self.market, "symbol_for", None)
        if not callable(resolve):
            raise ValueError("market_data_has_no_symbol_resolution")
        self._symbol = str(resolve(self.spec))

    # -- the one method the runner calls --------------------------------

    def poll(self) -> FeedEvent:
        now = self.clock()
        try:
            self._resolve()
            quote = self.market.quote(self.spec)
        except Exception as exc:
            self.error = getattr(exc, "reason", "") or type(exc).__name__
            detail = getattr(exc, "detail", "") or ""
            # A terminal that cannot answer is not "quiet": it is gone.
            return FeedEvent(
                DISCONNECTED if self.kind == KIND_TERMINAL else ERROR,
                at=now,
                reason=self.error,
                detail=detail,
            )
        try:
            quote_at = quote.timestamp_utc()
        except Exception as exc:
            return FeedEvent(INVALID, at=now, reason="invalid_quote_timestamp", detail=type(exc).__name__)

        age = now - quote_at
        if age > timedelta(seconds=float(self.policy.max_quote_age_seconds)):
            self._last_quote = quote
            return FeedEvent(
                STALE,
                at=now,
                quote=quote,
                reason="stale_tick",
                detail=f"age_seconds={age.total_seconds():.3f}",
            )
        if -age > timedelta(seconds=float(self.policy.max_future_skew_seconds)):
            return FeedEvent(
                INVALID,
                at=now,
                quote=quote,
                reason="tick_from_the_future",
                detail=f"skew_seconds={(-age).total_seconds():.3f}",
            )

        self._last_quote = quote
        index = self._window_index(quote_at)
        start = self._window_at(index)
        if self._window_start is None:
            self._window_start = start
            self._quotes = [quote]
            return FeedEvent(TICK, at=now, quote=quote)

        if index == self._window_index(self._window_start):
            self._quotes.append(quote)
            return FeedEvent(TICK, at=now, quote=quote)

        completed_start = self._window_start
        completed = self._quotes
        skipped = index - self._window_index(completed_start) - 1
        self._window_start = start
        self._quotes = [quote]

        if skipped > self.policy.max_gap_windows:
            self._skipped += skipped
            return FeedEvent(
                GAP,
                at=now,
                quote=quote,
                reason="data_gap",
                detail=f"skipped_windows={skipped}",
                skipped_windows=skipped,
            )
        try:
            bar = _bar_from_quotes(
                pair=str(getattr(self.spec, "canonical_pair", "")),
                window_start=completed_start,
                interval_seconds=self.policy.interval_seconds,
                quotes=completed,
                timeframe=self.policy.timeframe,
            )
        except ValueError as exc:
            return FeedEvent(INVALID, at=now, reason="bar_aggregation_failed", detail=str(exc))
        check = self.policy.freshness().check_instant(bar.last_quote_at, bar.end_at)
        if not check.ok:
            return FeedEvent(
                STALE,
                at=now,
                bar=bar,
                quote=quote,
                reason="bar_quote_stale",
                detail=check.reason.value,
            )
        return FeedEvent(BAR, at=now, bar=bar, quote=quote)

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "data_class": self.data_class,
            "symbol": self._symbol,
            "policy": self.policy.audit_view(),
            "last_tick_at": (
                self._last_quote.timestamp.isoformat() if self._last_quote else ""
            ),
            "skipped_windows": self._skipped,
            "label": "MetaTrader 5 terminal ticks (live market data)",
        }


class LateBoundMarketData:
    """A terminal data surface that may not exist yet.

    The workstation only has a quote surface once the operator presses Connect.
    Rather than build the feed at start-up and fail, the runner holds this proxy:
    every poll asks the factory for the *current* market object, so connecting
    later starts the feed, and disconnecting makes the next poll fail closed with
    ``DISCONNECTED`` instead of silently repeating the last price.

    ``factory`` must raise (not return ``None``) when there is no terminal; the
    error is reported by the feed, never swallowed.
    """

    kind = "LATE_BOUND_TERMINAL"

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory

    def _market(self) -> Any:
        market = self._factory()
        if market is None:
            raise ValueError("terminal_not_connected")
        return market

    def symbol_for(self, spec: Any) -> str:
        return self._market().symbol_for(spec)

    def quote(self, spec: Any) -> ForexQuote:
        return self._market().quote(spec)


# ---------------------------------------------------------------------------
# offline replay
# ---------------------------------------------------------------------------


class ReplayBarFeed:
    """A stored bar sequence, replayed one bar at a time.

    The executable price for the decision bar is the next bar's open.  The last
    bar therefore cannot be traded -- there is no later price -- and the runner
    reports that rather than inventing one.
    """

    kind = KIND_REPLAY

    def __init__(
        self,
        *,
        bars: Sequence[ForexQuoteBar],
        policy: FeedPolicy | None = None,
        data_class: str = "USER_SUPPLIED_HISTORICAL",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bars = tuple(bars)
        self.policy = policy or FeedPolicy(interval_seconds=(self.bars[0].duration_seconds if self.bars else 60))
        self.data_class = str(data_class)
        self.clock = clock or _utc_now
        self._index = -1

    @staticmethod
    def _open_quote(bar: ForexQuoteBar) -> ForexQuote:
        return ForexQuote(
            pair=bar.pair,
            timestamp=bar.timestamp,
            bid=bar.bid.open,
            ask=bar.ask.open,
            source=bar.source,
            quote_id=f"replay:{bar.timestamp.isoformat()}",
            received_at=bar.timestamp,
            metadata={"basis": "NEXT_BAR_OPEN", "data_class": "HISTORICAL_REPLAY"},
        )

    def poll(self) -> FeedEvent:
        if self._index + 1 >= len(self.bars):
            return FeedEvent(END, at=self.clock(), reason="replay_exhausted")
        self._index += 1
        bar = self.bars[self._index]
        following = self.bars[self._index + 1] if self._index + 1 < len(self.bars) else None
        # A stored gap is reported, never filled in.
        if following is not None:
            expected = bar.end_at
            if following.timestamp - expected > timedelta(
                seconds=self.policy.interval_seconds + 1
            ):
                return FeedEvent(
                    GAP,
                    at=self.clock(),
                    bar=bar,
                    reason="data_gap",
                    detail=f"next_bar_delay={(following.timestamp - expected)}",
                )
        quote = self._open_quote(following) if following is not None else None
        return FeedEvent(BAR, at=self.clock(), bar=bar, quote=quote)

    @property
    def exhausted(self) -> bool:
        return self._index + 1 >= len(self.bars)

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "data_class": self.data_class,
            "bars": len(self.bars),
            "cursor": self._index + 1,
            "policy": self.policy.audit_view(),
            "first_bar_at": self.bars[0].timestamp.isoformat() if self.bars else "",
            "last_bar_at": self.bars[-1].end_at.isoformat() if self.bars else "",
            "label": "Offline replay of a stored bar sequence (not live data)",
        }


__all__ = [
    "LateBoundMarketData",
    "BAR",
    "BAR_SOURCE",
    "BarFeed",
    "DISCONNECTED",
    "END",
    "ERROR",
    "FATAL_EVENTS",
    "FeedEvent",
    "FeedPolicy",
    "GAP",
    "INVALID",
    "KIND_REPLAY",
    "KIND_TERMINAL",
    "ReplayBarFeed",
    "STALE",
    "TICK",
    "TerminalBarFeed",
]
