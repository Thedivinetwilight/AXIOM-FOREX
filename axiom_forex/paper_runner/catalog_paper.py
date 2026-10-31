"""The Monday test: a live-market *paper* forward run of the catalog.

This is deliberately not the live path, and not the backtest either.  It reads
the terminal's own quotes and completed bars, decides with the same causal rules
the evaluation used, and fills **locally, in a ledger file**, at the quote that was
actually on screen.  No order leaves the process: the only object it is given is a
narrow read-only surface -- ``symbol_names``, ``symbol_info``, ``tick``, ``rates`` --
so there is no function in reach that could send, modify, or close anything.

What it refuses to do, by construction:

* refuse to start twice (a start while a run is live returns ``already_running``);
* refuse to start without a stored evaluation and a screened paper set;
* refuse to fill on a quote older than the freshness allowance (``stale_quote``),
  and record that a signal was skipped rather than trading a stale price;
* refuse a symbol the terminal no longer lists, or one without usable facts for a
  cost model;
* refuse to treat a short history as a finished evaluation: the run reports
  ``waiting_for_history`` and keeps collecting, it never claims a result.

What it cannot do: change the execution mode, arm live trading, read credentials,
touch the account, or produce a profit claim.  Every number it writes is a locally
simulated fill from a real broker quote, labelled as such.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from axiom_forex.config.terminal_facts import symbol_facts
from axiom_forex.research.engine import cost_model_for
from axiom_forex.research.series import build_series, rows_from_rates

from ..strategies.catalog.candidates import Candidate, catalog, timeframes_for
from ..strategies.catalog.indicators import IndicatorCache
from ..strategies.catalog.primitives import HigherTimeframe
from ..strategies.catalog.simulate import context_for, signals_for

# -- states -----------------------------------------------------------------
IDLE = "IDLE"
RUNNING = "RUNNING"
STOPPED = "STOPPED"
BLOCKED = "BLOCKED"

# -- reason codes -----------------------------------------------------------
REASON_ALREADY_RUNNING = "paper_test_already_running"
REASON_NO_WATCHES = "no_screened_candidate"
REASON_UNKNOWN_CANDIDATE = "candidate_not_in_catalog"
REASON_SYMBOL_UNAVAILABLE = "symbol_not_offered_by_terminal"
REASON_NO_FACTS = "symbol_facts_unavailable"
REASON_TIMEFRAME = "timeframe_not_offered_by_candidate"
REASON_TOO_MANY = "too_many_watches"
REASON_LEDGER = "ledger_not_writable"

# -- events -----------------------------------------------------------------
EVENT_STARTED = "paper_test_started"
EVENT_STOPPED = "paper_test_stopped"
EVENT_WAITING = "waiting_for_history"
EVENT_FILL = "simulated_fill"
EVENT_SIGNAL = "signal"
EVENT_SIGNAL_SKIPPED = "signal_skipped"
EVENT_STALE = "stale_quote"
EVENT_CLOSED = "simulated_close"
EVENT_MARKET = "market_closed"
EVENT_RESTORED = "position_restored"

#: Exit reasons a locally simulated fill can carry.
EXIT_SIGNAL = "SIGNAL_EXIT"
EXIT_OPPOSITE = "OPPOSING_ENTRY"
EXIT_STOP = "STOP_LOSS"
EXIT_TARGET = "TARGET"
EXIT_TRAIL = "TRAILING_STOP"
EXIT_TIME = "TIME_EXIT"
EXIT_OPERATOR = "OPERATOR_FLAT"

#: The freshness allowance.  A quote older than this is not a price to trade on.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 120.0

#: After this much quiet, the symbol is called closed rather than merely stale.
MARKET_CLOSED_SECONDS = 900.0

#: How many bars to keep in the decision window per watch.
DEFAULT_BAR_WINDOW = 800

#: A guard against a page handing in a thousand rows by accident.
MAX_WATCHES = 48

#: Default poll interval.  Bars are what decisions are made on, so a few seconds
#: is plenty and keeps a laptop quiet.
DEFAULT_POLL_SECONDS = 5.0

LEDGER_NAME = "catalog-paper-ledger.jsonl"
STATE_NAME = "catalog-paper-state.json"

NUMBER_OF_LEDGER_EVENTS = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _decimal(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Watch:
    """One evaluated row this test follows: a candidate, a symbol, a timeframe."""

    candidate_id: str
    symbol: str
    timeframe: str
    candidate: Candidate
    trades_in_evaluation: int = 0
    folds_positive: int = 0

    @property
    def key(self) -> str:
        return f"{self.candidate_id}@{self.symbol}|{self.timeframe}"

    @property
    def label(self) -> str:
        return f"{self.symbol} {self.timeframe} {self.candidate.family}"

    def audit_view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "family": self.candidate.family,
            "summary": self.candidate.summary,
            "entry": self.candidate.entry_text,
            "exit": self.candidate.exit_text,
            "filter": self.candidate.filter_text,
            "risk": self.candidate.risk_text,
            "trades_in_evaluation": self.trades_in_evaluation,
            "folds_positive": self.folds_positive,
        }


@dataclass
class Position:
    """A position this test opened, priced from the broker's own bid/ask."""

    key: str
    candidate_id: str
    symbol: str
    timeframe: str
    direction: int
    lots: float
    entry_at: str
    entry_price: float
    entry_mid: float
    atr: float
    stop: float | None
    target: float | None
    trail: float | None
    best: float
    notional_quote: float
    bar_index: int
    restored: bool = False

    def audit_view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": "LONG" if self.direction > 0 else "SHORT",
            "lots": self.lots,
            "entry_at": self.entry_at,
            "entry_price": round(self.entry_price, 8),
            "stop": None if self.stop is None else round(self.stop, 8),
            "target": None if self.target is None else round(self.target, 8),
            "trail": None if self.trail is None else round(self.trail, 8),
            "restored_after_restart": self.restored,
            "priced_from": "BROKER_TICK",
            "simulated": True,
        }


def watches_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    symbol_names: Sequence[str] = (),
    limit: int = 12,
    known: Mapping[str, Candidate] | None = None,
) -> tuple[tuple[Watch, ...], tuple[dict[str, str], ...]]:
    """Turn stored paper-set rows into watches, or say why each one cannot be.

    The rows come from the evaluation record, so a candidate here has been
    measured.  Everything else -- the terminal's symbol list, the catalog entry,
    the timeframe the candidate was declared for -- is checked again now, because
    a stored row is a claim about yesterday and the terminal is the authority today.
    """

    items = catalog() if known is None else None
    by_id: Mapping[str, Candidate] = known or {item.candidate_id: item for item in items or ()}
    offered = {str(name).strip() for name in symbol_names if str(name).strip()}
    limit = max(0, min(int(limit), MAX_WATCHES))
    chosen: list[Watch] = []
    refused: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        candidate_id = str(row.get("candidate_id") or "")
        symbol = str(row.get("symbol") or "")
        timeframe = str(row.get("timeframe") or "")
        candidate = by_id.get(candidate_id)
        if candidate is None:
            refused.append(
                {
                    "reason": REASON_UNKNOWN_CANDIDATE,
                    "detail": f"{candidate_id or '(empty id)'} is not in this build's catalog.",
                }
            )
            continue
        if offered and symbol not in offered:
            refused.append(
                {
                    "reason": REASON_SYMBOL_UNAVAILABLE,
                    "detail": f"The terminal does not offer {symbol or '(no symbol)'} right now.",
                }
            )
            continue
        if timeframe not in timeframes_for(candidate):
            refused.append(
                {
                    "reason": REASON_TIMEFRAME,
                    "detail": f"{candidate_id} is not declared for {timeframe}.",
                }
            )
            continue
        watch = Watch(
            candidate_id=candidate_id,
            symbol=symbol,
            timeframe=timeframe,
            candidate=candidate,
            trades_in_evaluation=int(row.get("trades_in_evaluation") or row.get("trades") or 0),
            folds_positive=int(row.get("folds_positive") or 0),
        )
        if watch.key in seen:
            continue
        seen.add(watch.key)
        chosen.append(watch)
        if len(chosen) >= max(0, limit):
            break
    return tuple(chosen), tuple(refused)


@dataclass
class _Stats:
    cycles: int = 0
    signals: int = 0
    skipped_stale: int = 0
    fills: int = 0
    closes: int = 0
    waiting: int = 0


class CatalogPaperTest:
    """A single, explicit, locally simulated forward test.  One at a time."""

    def __init__(
        self,
        *,
        quotes: Any,
        watches: Sequence[Watch],
        root: Path | str,
        lot: float = 0.10,
        max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        bar_window: int = DEFAULT_BAR_WINDOW,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._quotes = quotes
        self._watches = tuple(watches)
        self._root = Path(root)
        self._lot = float(lot)
        self._max_age = max(1.0, float(max_quote_age_seconds))
        self._poll = max(0.5, float(poll_seconds))
        self._bar_window = max(120, int(bar_window))
        self._clock = clock

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._status = IDLE
        self._started_at: str = ""
        self._stopped_at: str = ""
        self._reason: str = ""
        self._positions: dict[str, Position] = {}
        self._quotes_seen: dict[str, dict[str, Any]] = {}
        self._last_bar: dict[str, str] = {}
        self._last_decision: dict[str, dict[str, Any]] = {}
        self._fills: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._stats = _Stats()
        self._blockers: tuple[dict[str, str], ...] = ()
        self._ledger_path = self._root / LEDGER_NAME
        self._state_path = self._root / STATE_NAME

    # -- properties -------------------------------------------------------

    @property
    def ledger_path(self) -> Path:
        return self._ledger_path

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def status(self) -> str:
        return self._status

    @property
    def watches(self) -> tuple[Watch, ...]:
        return self._watches

    # -- guards -----------------------------------------------------------

    def blockers(self) -> tuple[dict[str, str], ...]:
        """Everything that would stop a start right now, in the UI's words."""

        found: list[dict[str, str]] = []
        if self.running:
            found.append(
                {
                    "reason": REASON_ALREADY_RUNNING,
                    "detail": "A live-market paper test is already running. Stop it "
                    "before starting another one.",
                }
            )
        if not self._watches:
            found.append(
                {
                    "reason": REASON_NO_WATCHES,
                    "detail": "No candidate passed the screen, so there is nothing to "
                    "paper trade yet. Run the catalog evaluation first.",
                }
            )
        if len(self._watches) > MAX_WATCHES:
            found.append(
                {
                    "reason": REASON_TOO_MANY,
                    "detail": f"{len(self._watches)} rows were requested; the test follows "
                    f"at most {MAX_WATCHES}.",
                }
            )
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            found.append(
                {
                    "reason": REASON_LEDGER,
                    "detail": "The paper ledger directory could not be created.",
                }
            )
        return tuple(found)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Begin the forward test, or return the reason it cannot begin."""

        with self._lock:
            if self.running:
                # A second Start must leave the live test exactly as it was.
                return {
                    "started": False,
                    "blockers": [
                        {
                            "reason": REASON_ALREADY_RUNNING,
                            "detail": "A live-market paper test is already running. Stop "
                            "it before starting another one.",
                        }
                    ],
                    "status": self._status,
                }
            blockers = self.blockers()
            if blockers:
                self._status = BLOCKED
                self._blockers = blockers
                self._reason = blockers[0]["reason"]
                return {"started": False, "blockers": list(blockers), "status": self._status}
            self._stop.clear()
            self._status = RUNNING
            self._reason = ""
            self._started_at = _iso(self._clock())
            self._stopped_at = ""
            self._blockers = ()
            self._restore_state()
            self._write_state()
            self._record(EVENT_STARTED, {"watches": len(self._watches)})
            self._thread = threading.Thread(
                target=self._loop, name="catalog-paper-test", daemon=True
            )
            self._thread.start()
            return {"started": True, "blockers": [], "status": self._status}

    def stop(self, *, reason: str = "") -> dict[str, Any]:
        """Ask the loop to stop; it finishes the bar it is on and writes state."""

        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)
        with self._lock:
            if self._status == RUNNING:
                self._status = STOPPED
                self._stopped_at = _iso(self._clock())
                self._reason = reason or "operator_stopped"
                self._record(EVENT_STOPPED, {"reason": self._reason})
                self._write_state()
            self._thread = None
            return {"status": self._status, "reason": self._reason}

    def close_all(self, *, reason: str = "operator_flat") -> dict[str, Any]:
        """Simulate closing every open position at the current quote.

        This is still paper: the fill is a number in the ledger.  It exists so a
        ledger is not left half-open when the operator stops the test.
        """

        with self._lock:
            closed = 0
            for key in list(self._positions):
                position = self._positions[key]
                quote = self._fresh_quote(position.symbol)
                if quote is None:
                    continue
                price = self._fill_price(position.direction, quote, closing=True)
                self._close(position, price, quote, EXIT_OPERATOR)
                closed += 1
            self._write_state()
            return {"closed": closed, "open": len(self._positions)}

    # -- the loop ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as error:  # curated: the message stays in the process
                with self._lock:
                    self._record(
                        "cycle_error",
                        {"detail_class": type(error).__name__},
                    )
                    self._status = BLOCKED
                    self._reason = "cycle_error"
            self._stop.wait(self._poll)
        with self._lock:
            if self._status == RUNNING:
                self._status = STOPPED
                self._stopped_at = _iso(self._clock())
                self._reason = self._reason or "loop_ended"
                self._record(EVENT_STOPPED, {"reason": self._reason})
            self._write_state()

    def _cycle(self) -> None:
        symbol_bars: dict[tuple[str, str], Any] = {}
        for watch in self._watches:
            if self._stop.is_set():
                return
            with self._lock:
                self._stats.cycles += 1
            quote = self._fresh_quote(watch.symbol)
            if quote is None:
                with self._lock:
                    self._stats.waiting += 1
                    self._last_decision[watch.key] = {
                        "at": _iso(self._clock()),
                        "action": "NO_TRADE",
                        "reason": EVENT_STALE,
                    }
                continue
            bars = self._bars_for(watch, symbol_bars)
            if bars is None:
                with self._lock:
                    self._last_decision[watch.key] = {
                        "at": _iso(self._clock()),
                        "action": "WAITING",
                        "reason": EVENT_WAITING,
                    }
                continue
            series = bars
            if len(series) < max(8, watch.candidate.min_bars):
                with self._lock:
                    self._stats.waiting += 1
                    self._last_decision[watch.key] = {
                        "at": _iso(self._clock()),
                        "action": "WAITING",
                        "reason": EVENT_WAITING,
                        "have_bars": len(series),
                        "need_bars": watch.candidate.min_bars,
                    }
                    self._record(
                        EVENT_WAITING,
                        {
                            "key": watch.key,
                            "have_bars": len(series),
                            "need_bars": watch.candidate.min_bars,
                        },
                    )
                continue
            stamp = _iso(series.timestamps[-1])
            with self._lock:
                if self._last_bar.get(watch.key) == stamp:
                    continue  # no new completed bar; nothing can have changed
                self._last_bar[watch.key] = stamp
            self._decide(watch, series, quote)

    # -- decisions --------------------------------------------------------

    def _decide(self, watch: Watch, series: Any, quote: Any) -> None:
        candidate = watch.candidate
        cache = IndicatorCache.of(series)
        coarse = None
        if candidate.filter_kind.needs_coarse:
            coarse = HigherTimeframe.build(series, int(candidate.filter_params.get("ratio", 4)))
        context = context_for(series, cache, candidate, coarse=coarse)
        signals = signals_for(candidate, context)
        index = len(series) - 1
        long_now = bool(signals.longs[index] and signals.allowed_long[index])
        short_now = bool(signals.shorts[index] and signals.allowed_short[index])
        exit_now = bool(signals.exit_events[index])
        atr = signals.atr[index]
        with self._lock:
            position = self._positions.get(watch.key)
            if position is not None:
                position.bar_index = index
                closed = self._manage(watch, position, series, quote, exit_now, long_now, short_now)
                if closed:
                    return
                if self._positions.get(watch.key) is not None:
                    self._last_decision[watch.key] = {
                        "at": _iso(self._clock()),
                        "action": "HOLD_OPEN",
                        "reason": "position_open",
                        "bar": _iso(series.timestamps[index]),
                    }
                    return
            if not (long_now or short_now):
                self._last_decision[watch.key] = {
                    "at": _iso(self._clock()),
                    "action": "NO_TRADE",
                    "reason": "no_entry_signal",
                    "bar": _iso(series.timestamps[index]),
                }
                return
            direction = 1 if long_now else -1
            price = self._fill_price(direction, quote, closing=False)
            facts = self._facts(watch.symbol)
            point = _decimal(getattr(facts, "point", None)) or 0.0
            contract = _decimal(getattr(facts, "contract_size", None)) or 0.0
            policy = candidate.policy()
            atr_value = float(atr) if atr else 0.0
            mid = (float(quote.bid) + float(quote.ask)) / 2.0
            position = Position(
                key=watch.key,
                candidate_id=watch.candidate_id,
                symbol=watch.symbol,
                timeframe=watch.timeframe,
                direction=direction,
                lots=self._lot,
                entry_at=_iso(self._clock()),
                entry_price=price,
                entry_mid=mid,
                atr=atr_value,
                stop=(
                    price - direction * policy.stop_atr * atr_value
                    if (policy.stop_atr and atr_value)
                    else None
                ),
                target=(
                    price + direction * policy.target_atr * atr_value
                    if (policy.target_atr and atr_value)
                    else None
                ),
                trail=(price if (policy.trail_atr and atr_value) else None),
                best=price,
                notional_quote=abs(mid * contract * self._lot),
                bar_index=index,
            )
            self._positions[watch.key] = position
            self._stats.signals += 1
            self._stats.fills += 1
            opened = self._fill_event(position, "OPEN", "ENTRY_SIGNAL", quote)
            self._fills.append(opened)
            self._append_ledger(opened)
            self._last_decision[watch.key] = {
                "at": _iso(self._clock()),
                "action": "BUY" if direction > 0 else "SELL",
                "reason": "entry_signal",
                "bar": _iso(series.timestamps[index]),
                "simulated": True,
                "priced_from": "BROKER_TICK",
            }
            self._write_state()

    def _manage(
        self,
        watch: Watch,
        position: Position,
        series: Any,
        quote: Any,
        exit_now: bool,
        long_now: bool,
        short_now: bool,
    ) -> bool:
        """Apply the candidate's own exit rules to one open position."""

        policy = watch.candidate.policy()
        index = len(series) - 1
        high = float(series.mid_high[index])
        low = float(series.mid_low[index])
        direction = position.direction
        if position.trail is not None and policy.trail_atr and position.atr:
            if direction > 0:
                position.best = max(position.best, high)
                position.trail = max(
                    position.trail, position.best - policy.trail_atr * position.atr
                )
            else:
                position.best = min(position.best, low)
                position.trail = min(
                    position.trail, position.best + policy.trail_atr * position.atr
                )
        hit_stop = position.stop is not None and (
            low <= position.stop if direction > 0 else high >= position.stop
        )
        hit_trail = (
            position.trail is not None
            and index > position.bar_index
            and (low <= position.trail if direction > 0 else high >= position.trail)
        )
        hit_target = position.target is not None and (
            high >= position.target if direction > 0 else low <= position.target
        )
        if hit_stop or hit_trail:
            level = float(position.stop if hit_stop else position.trail)  # type: ignore[arg-type]
            reason = EXIT_STOP if hit_stop else EXIT_TRAIL
            self._close(position, level, quote, reason)
            return True
        if hit_target and position.target is not None:
            self._close(position, float(position.target), quote, EXIT_TARGET)
            return True
        if exit_now:
            self._close(position, self._fill_price(direction, quote, closing=True), quote, EXIT_SIGNAL)
            return True
        opposite = (long_now and direction < 0) or (short_now and direction > 0)
        if opposite:
            self._close(position, self._fill_price(direction, quote, closing=True), quote, EXIT_OPPOSITE)
            return True
        return False

    # -- fills and ledger -------------------------------------------------

    def _fill_price(self, direction: int, quote: Any, *, closing: bool) -> float:
        """Buy at the ask, sell at the bid, with the cost model's slippage."""

        cost = self._cost_for(quote.symbol)
        slip = cost.slippage_price if cost is not None else 0.0
        if (direction > 0) != closing:
            return float(quote.ask) + slip
        return float(quote.bid) - slip

    def _fill_event(self, position: Position, kind: str, reason: str, quote: Any) -> dict[str, Any]:
        return {
            "at": _iso(self._clock()),
            "kind": EVENT_FILL if kind == "OPEN" else EVENT_CLOSED,
            "reason": reason,
            "key": position.key,
            "candidate_id": position.candidate_id,
            "symbol": position.symbol,
            "timeframe": position.timeframe,
            "direction": "LONG" if position.direction > 0 else "SHORT",
            "lots": position.lots,
            "price": round(position.entry_price, 8),
            "quote_bid": _decimal(getattr(quote, "bid", None)),
            "quote_ask": _decimal(getattr(quote, "ask", None)),
            "quote_time": _iso(getattr(quote, "time", None)),
            "simulated": True,
            "priced_from": "BROKER_TICK",
        }

    def _close(self, position: Position, price: float, quote: Any, reason: str) -> None:
        contract = _decimal(getattr(self._facts(position.symbol), "contract_size", None)) or 0.0
        point = _decimal(getattr(self._facts(position.symbol), "point", None)) or 0.0
        gross = position.direction * (price - position.entry_price) * contract * position.lots
        mid_move = position.direction * (
            (float(quote.bid) + float(quote.ask)) / 2.0 - position.entry_mid
        )
        event = {
            "at": _iso(self._clock()),
            "kind": EVENT_CLOSED,
            "reason": reason,
            "key": position.key,
            "candidate_id": position.candidate_id,
            "symbol": position.symbol,
            "timeframe": position.timeframe,
            "direction": "LONG" if position.direction > 0 else "SHORT",
            "lots": position.lots,
            "entry_price": round(position.entry_price, 8),
            "exit_price": round(price, 8),
            "simulated_net_quote": round(gross, 4),
            "simulated_mid_move_pips": round(mid_move / point, 1) if point else 0.0,
            "quote_bid": _decimal(getattr(quote, "bid", None)),
            "quote_ask": _decimal(getattr(quote, "ask", None)),
            "quote_time": _iso(getattr(quote, "time", None)),
            "restored_after_restart": position.restored,
            "simulated": True,
            "priced_from": "BROKER_TICK",
        }
        self._fills.append(event)
        self._append_ledger(event)
        self._positions.pop(position.key, None)
        self._stats.closes += 1
        self._record(EVENT_CLOSED, {"key": position.key, "reason": reason})
        self._write_state()

    def _record(self, kind: str, fields: Mapping[str, Any]) -> None:
        event = {"at": _iso(self._clock()), "kind": kind}
        event.update({key: value for key, value in fields.items()})
        self._events.append(event)
        if len(self._events) > 500:
            self._events = self._events[-500:]
        self._append_ledger(event)

    def _append_ledger(self, event: Mapping[str, Any]) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with self._ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        except OSError:
            self._reason = REASON_LEDGER

    def _write_state(self) -> None:
        payload = {
            "version": 1,
            "updated_at": _iso(self._clock()),
            "status": self._status,
            "running": self.running,
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
            "reason": self._reason,
            "lots": self._lot,
            "max_quote_age_seconds": self._max_age,
            "watches": [watch.audit_view() for watch in self._watches],
            "stats": {
                "cycles": self._stats.cycles,
                "signals": self._stats.signals,
                "fills": self._stats.fills,
                "closes": self._stats.closes,
                "waiting": self._stats.waiting,
            },
            "positions": [position.audit_view() for position in self._positions.values()],
            "last_bar": dict(self._last_bar),
        }
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_name(self._state_path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError:
            self._reason = REASON_LEDGER

    def _restore_state(self) -> None:
        """Resume open positions after a restart, and say so in the ledger."""

        self._positions = {}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        for entry in payload.get("positions") or ():
            if not isinstance(entry, Mapping):
                continue
            key = str(entry.get("key") or "")
            if not key:
                continue
            try:
                position = Position(
                    key=key,
                    candidate_id=str(entry.get("candidate_id") or ""),
                    symbol=str(entry.get("symbol") or ""),
                    timeframe=str(entry.get("timeframe") or ""),
                    direction=1 if str(entry.get("direction")) == "LONG" else -1,
                    lots=float(entry.get("lots") or self._lot),
                    entry_at=str(entry.get("entry_at") or ""),
                    entry_price=float(entry.get("entry_price") or 0.0),
                    entry_mid=float(entry.get("entry_price") or 0.0),
                    atr=0.0,
                    stop=None if entry.get("stop") is None else float(entry["stop"]),
                    target=None if entry.get("target") is None else float(entry["target"]),
                    trail=None if entry.get("trail") is None else float(entry["trail"]),
                    best=float(entry.get("entry_price") or 0.0),
                    notional_quote=0.0,
                    bar_index=0,
                    restored=True,
                )
            except (TypeError, ValueError):
                continue
            self._positions[key] = position
            self._record(EVENT_RESTORED, {"key": key})

    # -- inputs -----------------------------------------------------------

    def _facts(self, symbol: str) -> Any:
        """One symbol's facts, however this quote source reports them.

        The terminal returns a *sequence of raw records*; a test double may return
        one object.  Both are normalised here, and a record that does not carry the
        fields a cost model needs stays unknown rather than being defaulted.
        """

        try:
            raw = self._quotes.symbol_info(symbol)
        except Exception:  # a missing fact is reported, never guessed
            return None
        entries = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        for entry in entries:
            if entry is None:
                continue
            if getattr(entry, "point", None) is not None and getattr(
                entry, "contract_size", None
            ) is not None and not isinstance(entry, Mapping):
                return entry
            captured = symbol_facts(entry)
            if captured is None:
                continue
            if not captured.broker_symbol:
                captured = replace(captured, broker_symbol=symbol)
            if captured.point is None or captured.contract_size is None:
                return None
            return captured
        return None

    def _cost_for(self, symbol: str) -> Any:
        facts = self._facts(symbol)
        if facts is None:
            return None
        point = _decimal(getattr(facts, "point", None)) or 0.0
        contract = _decimal(getattr(facts, "contract_size", None)) or 0.0
        if point <= 0 or contract <= 0:
            return None
        return cost_model_for(facts, account_currency=str(getattr(facts, "account_currency", "") or ""))

    def _fresh_quote(self, symbol: str) -> Any:
        try:
            quote = self._quotes.tick(symbol)
        except Exception:
            with self._lock:
                self._quotes_seen[symbol] = {
                    "at": _iso(self._clock()),
                    "state": "unavailable",
                    "age_seconds": None,
                }
            return None
        when = getattr(quote, "time", None)
        age = None
        if isinstance(when, datetime):
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            age = max(0.0, (self._clock() - when).total_seconds())
        bid = _decimal(getattr(quote, "bid", None))
        ask = _decimal(getattr(quote, "ask", None))
        with self._lock:
            self._quotes_seen[symbol] = {
                "at": _iso(self._clock()),
                "state": (
                    "fresh"
                    if (age is not None and age <= self._max_age)
                    else ("stale" if age is not None else "unknown_age")
                ),
                "age_seconds": None if age is None else round(age, 1),
                "bid": bid,
                "ask": ask,
            }
            if bid is None or ask is None or bid <= 0 or ask < bid:
                return None
            if age is None or age > self._max_age:
                self._stats.skipped_stale += 1
                self._record(
                    EVENT_STALE,
                    {
                        "symbol": symbol,
                        "age_seconds": None if age is None else round(age, 1),
                        "allowance_seconds": self._max_age,
                    },
                )
                return None
        return quote

    def _bars_for(self, watch: Watch, cache: dict[tuple[str, str], Any]) -> Any:
        key = (watch.symbol, watch.timeframe)
        if key in cache:
            return cache[key]
        series = self._fetch_bars(watch)
        cache[key] = series
        return series

    def _fetch_bars(self, watch: Watch) -> Any:
        facts = self._facts(watch.symbol)
        point = _decimal(getattr(facts, "point", None)) if facts is not None else None
        seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}.get(
            watch.timeframe, 0
        )
        if not seconds:
            return None
        want = max(self._bar_window, watch.candidate.min_bars + 50)
        end = self._clock()
        start = end - timedelta(seconds=seconds * want * 3)
        try:
            rows = self._quotes.rates(watch.symbol, watch.timeframe, start, end)
        except Exception:
            return None
        if not rows:
            return None
        rates = rows_from_rates(rows)
        usable = [row for row in rates if row.source != "UNUSABLE"]
        if len(usable) > want:
            rates = rates[len(rates) - want :]
        pair = str(getattr(facts, "pair", "") or watch.symbol)
        series = build_series(
            watch.symbol,
            pair,
            watch.timeframe,
            seconds,
            Decimal(str(point)) if point else None,
            rates,
        )
        return series if len(series) else None

    # -- views -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            age_limit = self._max_age
            quotes = []
            for symbol, seen in sorted(self._quotes_seen.items()):
                age = seen.get("age_seconds")
                quotes.append(
                    {
                        "symbol": symbol,
                        "state": seen.get("state") or "unknown",
                        "age_seconds": age,
                        "fresh": bool(age is not None and age <= age_limit),
                        "checked_at": seen.get("at") or "",
                    }
                )
            closed = [event for event in self._fills if event.get("kind") == EVENT_CLOSED]
            return {
                "status": self._status,
                "running": self.running,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "reason": self._reason,
                "simulated": True,
                "priced_from": "BROKER_TICK",
                "lots": self._lot,
                "max_quote_age_seconds": age_limit,
                "watches": [watch.audit_view() for watch in self._watches],
                "active_symbols": sorted({watch.symbol for watch in self._watches}),
                "quotes": quotes,
                "signals": [dict(self._last_decision[key]) | {"key": key} for key in self._last_decision],
                "orders": [event for event in self._fills if event.get("kind") == EVENT_FILL][
                    -NUMBER_OF_LEDGER_EVENTS:
                ],
                "fills": closed[-NUMBER_OF_LEDGER_EVENTS:],
                "positions": [position.audit_view() for position in self._positions.values()],
                "events": [dict(event) for event in self._events[-NUMBER_OF_LEDGER_EVENTS:]],
                "stats": {
                    "cycles": self._stats.cycles,
                    "signals": self._stats.signals,
                    "fills": self._stats.fills,
                    "closes": self._stats.closes,
                    "stale_quotes_skipped": self._stats.skipped_stale,
                    "waiting_for_history": self._stats.waiting,
                },
                "blockers": list(self.blockers()),
                "ledger": str(self._ledger_path),
                "state": str(self._state_path),
                "claim": (
                    "Locally simulated fills on the terminal's own quotes. No order was "
                    "sent, no account was touched, and nothing here is a profit claim."
                ),
            }


def summarise(snapshot: Mapping[str, Any]) -> str:
    """A single line for the page header."""

    status = str(snapshot.get("status") or "")
    positions = len(snapshot.get("positions") or ())
    fills = len(snapshot.get("fills") or ())
    symbols = ", ".join(snapshot.get("active_symbols") or ()) or "no symbols"
    return f"{status}: {fills} simulated close(s), {positions} open, watching {symbols}"


__all__ = [
    "BLOCKED",
    "CatalogPaperTest",
    "DEFAULT_MAX_QUOTE_AGE_SECONDS",
    "IDLE",
    "MAX_WATCHES",
    "RUNNING",
    "STOPPED",
    "Watch",
    "summarise",
    "watches_from_rows",
]
