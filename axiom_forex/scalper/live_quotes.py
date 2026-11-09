"""Read live MT5 quotes through an already-connected workstation session.

This module deliberately cannot connect to MT5: callers hand it the existing
observation session after the operator presses Connect. It accepts no trading
adapter and imports no order, check, close, or account-mutation interface.
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..brokers.mt5.terminal import MT5DataRefused


class LiveQuoteRefused(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class LiveQuote:
    symbol: str
    bid: Any
    ask: Any
    source_time: datetime
    received_at: datetime
    received_monotonic_ns: int
    source: str = "MT5_LIVE_PAPER"


class MT5LiveQuoteSource:
    """Narrow read-only quote handoff from the workstation's connected session."""

    def __init__(self, session: Any, *, clock: Callable[[], datetime] | None = None,
                 monotonic_ns: Callable[[], int] = time.monotonic_ns,
                 max_quote_age_ms: int = 1500):
        if max_quote_age_ms <= 0:
            raise LiveQuoteRefused("invalid_freshness_limit")
        self._session = session
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_ns = monotonic_ns
        self._max_quote_age_ms = max_quote_age_ms

    def read(self, symbol: str) -> LiveQuote:
        if not symbol or not isinstance(symbol, str):
            raise LiveQuoteRefused("symbol_missing")
        # Never trigger connect, even if the session offers one.
        if not bool(getattr(self._session, "connected", False)):
            raise LiveQuoteRefused("terminal_not_connected")
        terminal = self._session.market_data_source()
        if terminal is None:
            raise LiveQuoteRefused("market_data_unavailable")
        try:
            tick = terminal.tick(symbol)
        except MT5DataRefused as exc:
            raise LiveQuoteRefused(exc.reason) from exc
        except Exception as exc:
            raise LiveQuoteRefused(f"quote_read_failed:{type(exc).__name__}") from exc
        # A disconnect racing the read must not leak a just-returned quote into a
        # new decision. The next explicit Connect creates a new session epoch.
        if not bool(getattr(self._session, "connected", False)):
            raise LiveQuoteRefused("terminal_disconnected_during_read")
        bid, ask, stamp = getattr(tick, "bid", None), getattr(tick, "ask", None), getattr(tick, "time", None)
        if bid is None or ask is None or stamp is None:
            raise LiveQuoteRefused("quote_fields_missing")
        returned_symbol = str(getattr(tick, "symbol", symbol))
        if returned_symbol != symbol:
            raise LiveQuoteRefused("quote_symbol_mismatch")
        if not isinstance(stamp, datetime):
            raise LiveQuoteRefused("quote_time_malformed")
        try:
            if bid <= 0 or ask <= 0 or ask < bid:
                raise LiveQuoteRefused("quote_bid_ask_invalid")
        except TypeError as exc:
            raise LiveQuoteRefused("quote_bid_ask_malformed") from exc
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        received = self._clock()
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        age_ms = (received - stamp).total_seconds() * 1000
        if age_ms < -self._max_quote_age_ms:
            raise LiveQuoteRefused("quote_from_future")
        if age_ms > self._max_quote_age_ms:
            raise LiveQuoteRefused("stale_quote")
        return LiveQuote(str(getattr(tick, "symbol", symbol)), bid, ask, stamp,
                         received, self._monotonic_ns())


class LivePaperPump:
    """Explicitly started PAPER quote poller; disconnect pauses and never reconnects."""

    def __init__(self, *, source: MT5LiveQuoteSource, desk: Any, symbol: str,
                 interval_seconds: float = 0.1,
                 monotonic: Callable[[], float] = time.monotonic,
                 persist: Callable[[], Any] | None = None):
        if getattr(getattr(desk, "config", None), "mode", None) != "PAPER":
            raise LiveQuoteRefused("scalper_is_paper_only")
        if not symbol or interval_seconds <= 0:
            raise LiveQuoteRefused("invalid_runner_settings")
        self.source, self.desk, self.symbol = source, desk, symbol
        # The adapter's freshness gate must be no looser than TickCore's policy;
        # otherwise repeat polling could keep refreshing the local receipt stamp.
        desk_age = int(getattr(getattr(desk, "config", None), "max_quote_age_ms", 0) or 0)
        if desk_age <= 0:
            raise LiveQuoteRefused("freshness_limit_unavailable")
        source._max_quote_age_ms = min(source._max_quote_age_ms, desk_age)
        self.interval_seconds = interval_seconds
        self._persist = persist
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._state = "STOPPED"
        self._reason = ""
        self._last_quote: LiveQuote | None = None
        self._last_decision: str = "not_started"

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "reason": "already_running", **self.status()}
            if not self.source._session.connected:
                self._state, self._reason = "BLOCKED", "terminal_not_connected"
                return {"ok": False, "reason": self._reason, **self.status()}
            self._stop.clear()
            self._state, self._reason = "RUNNING", ""
            self._thread = threading.Thread(target=self._run, name="axiom-scalper-paper", daemon=True)
            self._thread.start()
            return {"ok": True, **self.status()}

    def stop(self, reason: str = "operator_stop") -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval_seconds * 4))
        with self._lock:
            if self._state in {"RUNNING", "PAUSED"}:
                self._state, self._reason = "STOPPED", reason
        return {"ok": True, **self.status()}

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.source._session.connected:
                with self._lock:
                    self._state, self._reason = "PAUSED", "terminal_disconnected"
                return
            try:
                quote = self.source.read(self.symbol)
                with self._lock:
                    outcome = self.desk.on_tick(symbol=quote.symbol, bid=quote.bid, ask=quote.ask,
                                                source_time=quote.source_time, received_at=quote.received_at,
                                                source=quote.source, volume=None)
                    if outcome.accepted:
                        self._last_quote = quote
                    self._last_decision = (outcome.rejection or outcome.blocked or "quote_processed")
                    if self._persist is not None:
                        try:
                            self._persist()
                        except Exception as exc:
                            self._state, self._reason = "BLOCKED", "paper_ledger_write_failed"
                            self._last_decision = f"paper_ledger_write_failed:{type(exc).__name__}"
                            return
            except LiveQuoteRefused as exc:
                self._last_decision = exc.reason
                try:
                    self.desk.scribe.reject(
                        at_ms=int(time.time() * 1000), stage="live_quote_source",
                        reason=exc.reason, actor="workstation", symbol=self.symbol,
                        source="MT5_LIVE_PAPER",
                    )
                    if self._persist is not None:
                        self._persist()
                except Exception as write_exc:
                    with self._lock:
                        self._state, self._reason = "BLOCKED", "paper_ledger_write_failed"
                        self._last_decision = f"paper_ledger_write_failed:{type(write_exc).__name__}"
                    return
                with self._lock:
                    self._state, self._reason = "PAUSED", exc.reason
                return
            except Exception as exc:
                self._last_decision = f"decision_refused:{type(exc).__name__}"
                with self._lock:
                    self._state, self._reason = "BLOCKED", "decision_processing_failed"
                return
            self._stop.wait(self.interval_seconds)

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._monotonic()
            quote = self._last_quote
            try:
                desk_status = self.desk.status()
            except Exception:
                desk_status = {}
            stats = desk_status.get("stats", {}) if isinstance(desk_status, dict) else {}
            tick_stats = desk_status.get("ticks", {}) if isinstance(desk_status, dict) else {}
            quality = desk_status.get("quality", {}) if isinstance(desk_status, dict) else {}
            book = getattr(self.desk, "book", None)
            positions = getattr(book, "open_positions", ())
            unrealized_values: list[Any] = []
            if quote is not None:
                for position in positions:
                    point_value = getattr(position, "point_value_per_lot", None)
                    point = getattr(position, "point", None)
                    if point_value is None or point is None or point <= 0:
                        unrealized_values = []
                        break
                    mark = quote.bid if position.side == "BUY" else quote.ask
                    sign = Decimal(1) if position.side == "BUY" else Decimal(-1)
                    points = (Decimal(str(mark)) - position.entry_price) / point * sign
                    unrealized_values.append(points * point_value * position.lots)
            unrealized = (str(sum(unrealized_values, Decimal(0)))
                          if quote is not None and len(unrealized_values) == len(positions)
                          else None)
            equity = desk_status.get("equity") if isinstance(desk_status, dict) else None
            starting_equity = desk_status.get("starting_equity") if isinstance(desk_status, dict) else None
            realized = (str(Decimal(str(equity)) - Decimal(str(starting_equity)))
                        if equity is not None and starting_equity is not None else None)
            return {
                "mode": "PAPER", "source": "MT5_LIVE_PAPER", "state": self._state,
                "reason": self._reason, "symbol": self.symbol,
                "quote_age_seconds": None if quote is None else max(0.0, now - quote.received_monotonic_ns / 1e9),
                "quote_time": None if quote is None else quote.source_time.isoformat(),
                "last_decision": self._last_decision,
                "paper_orders": int(stats.get("orders", 0)),
                "paper_fills": int(stats.get("fills", 0)),
                "open_positions": len(positions),
                "positions": [item.audit_view() for item in positions],
                "ticks": dict(tick_stats),
                "accepted_ticks_by_symbol": {self.symbol: int(tick_stats.get("accepted", 0))},
                "quality_by_symbol": dict(quality),
                "paper_equity": equity,
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "pnl_availability": "calculated_from_last_accepted_quote" if unrealized is not None else "unavailable_until_point_value_and_quote_are_available",
                "paper_only": True,
            }
