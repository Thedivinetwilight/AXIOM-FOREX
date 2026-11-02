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
import math
import threading
import time
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
#: A stored paper position cannot be continued exactly.  Starting is refused
#: rather than resumed with a substituted zero or an invented stop.
REASON_STATE_RECOVERY = "paper_state_incomplete"

# -- events -----------------------------------------------------------------
EVENT_STARTED = "paper_test_started"
EVENT_STOPPED = "paper_test_stopped"
EVENT_WAITING = "waiting_for_history"
EVENT_FILL = "simulated_fill"
EVENT_SIGNAL = "signal"
EVENT_SIGNAL_SKIPPED = "signal_skipped"
EVENT_STALE = "stale_quote"
EVENT_CLOSED = "simulated_close"
#: A *note* that a close happened, kept out of the close-event kind on purpose.
#: Writing the summary under the same kind as the priced close made the ledger
#: contain two "simulated_close" entries per trade -- one with both prices and one
#: without -- which is exactly how a ledger starts counting a trade twice.
EVENT_CLOSE_RECORDED = "close_recorded"
EVENT_MARKET = "market_closed"
EVENT_RESTORED = "position_restored"
#: A stored position was resumed from an older schema with a *stated* derivation.
EVENT_MIGRATED = "state_migrated"
#: A stored position could not be resumed; the run stays blocked.
EVENT_RECOVERY_FAILED = "state_recovery_failed"

#: Exit reasons a locally simulated fill can carry.
EXIT_SIGNAL = "SIGNAL_EXIT"
EXIT_OPPOSITE = "OPPOSING_ENTRY"
EXIT_STOP = "STOP_LOSS"
EXIT_TARGET = "TARGET"
EXIT_TRAIL = "TRAILING_STOP"
EXIT_TIME = "TIME_EXIT"
EXIT_OPERATOR = "OPERATOR_FLAT"

#: How the price of a simulated close was arrived at.  ``BID_ASK`` means the
#: closing side of the spread was used and the modelled slippage was applied;
#: ``LEVEL`` means a stop, trailing stop or target level was touched, so no
#: spread and no slippage were applied to that leg.  The distinction is written
#: into the ledger because it decides what the P&L figure does and does not
#: include.
BASIS_BID_ASK = "BID_ASK"
BASIS_LEVEL = "LEVEL"

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

#: The state document's schema version.
#:
#: Version 2 records every field the exit logic reads -- the ATR the trailing stop
#: is measured in, the bar index the trail trigger compares against, the initial
#: and active stop/target/trail, and the trailing extreme -- so a restart resumes
#: a position *exactly*.  Version 1 records (written by the first build) omitted
#: all of them, which is how a restart could change an exit: ``atr=0.0`` alone
#: silently disabled the trailing stop.
#:
#: Version 3 adds the identity of the **entry bar**.  Without it the trailing
#: stop's "not on the entry bar" guard had nothing to compare against and read a
#: field the decision had just overwritten, so a trail could never fire.
STATE_VERSION = 3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _parse_iso(value: Any) -> datetime | None:
    """One stored timestamp, as an aware datetime, or ``None``.

    Every instant this module writes is ISO-8601 UTC.  A value it cannot parse
    is treated as absent: an unreadable time is not a reason to call a quote
    fresh.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _decimal(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _finite_positive(value: Any) -> bool:
    """A real, positive, finite number -- the only kind a price term may be."""

    number = _decimal(value)
    return number is not None and math.isfinite(number) and number > 0


def _after_entry_bar(entry_bar_at: Any, current_bar_at: Any) -> bool:
    """Is the bar being managed strictly later than the bar the trade opened on?

    The evaluation this test mirrors says ``index > entry_index``: a stop or trail
    may act from the next bar, never on the bar that opened the position.  Indices
    cannot be compared across paper cycles -- the series is rebuilt from a sliding
    rate request every cycle -- so the comparison is made on the bars' own stamps.

    An unparseable or missing stamp answers ``False``.  That is the conservative
    direction: an unknown bar identity may *delay* a trailing exit by a bar, but it
    can never fabricate one.
    """

    entry = _parse_iso(entry_bar_at)
    current = _parse_iso(current_bar_at)
    if entry is None or current is None:
        return False
    return current > entry


def _state_number(value: Any) -> float | None:
    """One stored number, or ``None`` when it is absent or not a usable number.

    A missing field and a corrupt field are the same answer here -- "not known" --
    because both mean the record cannot be resumed from its own contents.  Booleans
    are refused: ``True`` is not a price.
    """

    if value is None or isinstance(value, bool):
        return None
    number = _decimal(value)
    if number is None or not math.isfinite(number):
        return None
    return number


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
    #: The levels the position opened with, before any trailing ratchet.  Kept so
    #: a resumed position can be checked against the record it came from, and so
    #: the page can say what the trade was *meant* to risk.
    stop_initial: float | None = None
    target_initial: float | None = None
    trail_initial: float | None = None
    #: The stamp of the completed bar the last decision in this position ran on.
    bar_at: str = ""
    #: True when this position was resumed from an older state schema; ``migration``
    #: says exactly what was derived and why it cannot change an exit.
    migrated: bool = False
    migration: str = ""
    #: The completed bar the position was opened on.  ``bar_index``/``bar_at`` are
    #: the *management cursor* and move on every decision; this pair never moves.
    #: The trailing stop is armed only strictly after this bar -- exactly the rule
    #: the evaluation applies (``simulate.py``: ``index > entry_index``), so the
    #: paper test cannot stop out on the bar it entered on, and cannot refuse to
    #: stop out later either.
    entry_bar_at: str = ""
    entry_bar_index: int = -1
    #: The pair's quote (profit) currency and contract size, exactly as the
    #: terminal reported them when the position was opened.  A money figure is
    #: meaningless without its currency, and recovering these later would mean
    #: asking a terminal that may no longer be connected -- or guessing.  They
    #: travel with the position (and with its ledger events) instead.
    quote_currency: str = ""
    contract_size: float = 0.0

    def state_view(self) -> dict[str, Any]:
        """Everything the exit logic reads, in one document.

        This is the state a restart resumes from.  It is deliberately wider than
        :meth:`audit_view` (which is for a page): a page needs a readable summary,
        whereas a restart needs every number the next ``_manage`` call will compare
        a bar's high and low against.
        """

        return {
            "state_version": STATE_VERSION,
            "key": self.key,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": "LONG" if self.direction > 0 else "SHORT",
            "lots": self.lots,
            "entry_at": self.entry_at,
            "entry_price": self.entry_price,
            "entry_mid": self.entry_mid,
            # -- what the exit rules consume --------------------------------
            "atr": self.atr,
            "bar_index": self.bar_index,
            "bar_at": self.bar_at,
            "entry_bar_at": self.entry_bar_at,
            "entry_bar_index": self.entry_bar_index,
            "stop": self.stop,
            "target": self.target,
            "trail": self.trail,
            "best": self.best,
            "stop_initial": self.stop_initial,
            "target_initial": self.target_initial,
            "trail_initial": self.trail_initial,
            "notional_quote": self.notional_quote,
            "quote_currency": self.quote_currency,
            "contract_size": self.contract_size,
            "restored_after_restart": self.restored,
            "migrated": self.migrated,
            "migration": self.migration,
            "priced_from": "BROKER_TICK",
            "simulated": True,
        }

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
            "quote_currency": self.quote_currency,
            "contract_size": self.contract_size,
            "restored_after_restart": self.restored,
            # The management levels a reader needs to judge what a restart did.
            "atr": self.atr,
            "bar_index": self.bar_index,
            "bar_at": self.bar_at,
            "entry_bar_at": self.entry_bar_at,
            "best": self.best,
            "stop_initial": self.stop_initial,
            "target_initial": self.target_initial,
            "trail_initial": self.trail_initial,
            "migrated": self.migrated,
            "migration": self.migration,
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
    for position, row in enumerate(rows):
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
            # A paper test follows a bounded number of rows at a time.  Stopping here
            # used to drop the rest of the screened rows in silence, so the page said
            # "a paper test over 12 rows" as if 12 were all there were.  The rows left
            # over are now named.
            remaining = len(rows) - position - 1
            if remaining > 0:
                refused.append(
                    {
                        "reason": REASON_TOO_MANY,
                        "detail": (
                            f"{remaining} more stored row(s) were not followed: one paper "
                            f"test follows at most {max(0, limit)} at a time."
                        ),
                    }
                )
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
        confirmation: Callable[[Watch, int], Any] | None = None,
    ) -> None:
        self._quotes = quotes
        #: An optional *veto only* second opinion (Kronos, when the operator turned
        #: it on).  It is handed the watch and the direction a signal proposes, and
        #: may only say no.  ``None`` -- the default -- means no second opinion is
        #: consulted at all, which is the behaviour this class always had.  It is
        #: called while the loop holds its state lock, so it must be quick: the
        #: launcher's reader looks up one stored row and never runs a model.
        self._confirmation = confirmation
        #: Observed decision latency, in milliseconds.  Measured, never promised:
        #: the page shows these numbers with the machine they were measured on.
        self._latency_ms: dict[str, float] = {"last": 0.0, "min": 0.0, "max": 0.0, "total": 0.0}
        self._latency_samples = 0
        self._vetoed = 0
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
        #: What the last state read said.  ``{"ok": True}`` until a read finds
        #: something it cannot resume exactly.
        self._recovery: dict[str, Any] = {
            "ok": True,
            "version": 0,
            "restored": 0,
            "migrated": 0,
            "failed": [],
            "detail": "",
        }
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
        if not self._recovery.get("ok", True):
            found.append(self._recovery_blocker())
        return tuple(found)

    def _recovery_blocker(self) -> dict[str, str]:
        """The one sentence a page needs when a stored position cannot be resumed."""

        failed = self._recovery.get("failed") or []
        names = ", ".join(
            f"{item.get('symbol') or item.get('key') or 'a position'} "
            f"({item.get('reason') or 'incomplete state'})"
            for item in failed[:4]
        )
        more = "" if len(failed) <= 4 else f", and {len(failed) - 4} more"
        return {
            "reason": REASON_STATE_RECOVERY,
            "detail": (
                "The paper test will not resume: a stored position cannot be "
                "continued exactly, and it will not be continued with a substituted "
                "value. "
                + (f"{self._recovery.get('detail')}. " if self._recovery.get("detail") else "")
                + (f"Affected: {names}{more}. " if names else "")
                + "The state file has been left untouched. Review the paper ledger, "
                "then either flatten with \"Simulate closing open positions\" or "
                "restore the missing fields before starting again."
            ),
        }

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
            # Resume *before* claiming to run.  If any stored position cannot be
            # resumed exactly, refuse to start: a position managed with a
            # substituted ATR or stop would exit somewhere the uninterrupted run
            # never would, and the ledger would record that as a trade.
            recovery = self._restore_state()
            if not recovery["ok"]:
                self._status = BLOCKED
                self._reason = REASON_STATE_RECOVERY
                self._blockers = (self._recovery_blocker(),)
                # The state file is left exactly as it was found: it is the
                # evidence of what could not be resumed.
                return {
                    "started": False,
                    "blockers": list(self._blockers),
                    "status": self._status,
                    "recovery": recovery,
                }
            self._stop.clear()
            self._status = RUNNING
            self._reason = ""
            self._started_at = _iso(self._clock())
            self._stopped_at = ""
            self._blockers = ()
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
            began = time.monotonic()
            self._decide(watch, series, quote)
            self._note_latency((time.monotonic() - began) * 1000.0)

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
                position.bar_at = _iso(series.timestamps[index])
                closed = self._manage(watch, position, series, quote, exit_now, long_now, short_now)
                if closed:
                    return
                if self._positions.get(watch.key) is not None:
                    # Persist the *managed* state, not just the state at entry.
                    # A trailing stop ratchets inside ``_manage``; if that ratchet
                    # only lived in memory, a restart would resume a looser trail
                    # than the one the running test is honouring -- a restart would
                    # change the exit.  One small atomic write per completed bar.
                    self._write_state()
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
            if self._confirmation is not None:
                # A refusal here can only skip the trade.  It cannot reverse the
                # direction, resize it, or reach an order path.
                verdict = self._confirmation(watch, direction)
                allowed = bool(getattr(verdict, "allowed", True))
                if not allowed:
                    self._vetoed += 1
                    view = getattr(verdict, "audit_view", None)
                    detail = dict(view()) if callable(view) else {"reason": str(verdict)}
                    self._last_decision[watch.key] = {
                        "at": _iso(self._clock()),
                        "action": "NO_TRADE",
                        "reason": str(detail.get("reason") or "confirmation_refused"),
                        "detail": str(detail.get("detail") or ""),
                        "bar": _iso(series.timestamps[index]),
                        "simulated": True,
                    }
                    self._record(EVENT_SIGNAL_SKIPPED, {"key": watch.key, **detail})
                    return
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
                quote_currency=str(getattr(facts, "profit_currency", "") or "").upper(),
                contract_size=float(contract),
                stop_initial=(
                    price - direction * policy.stop_atr * atr_value
                    if (policy.stop_atr and atr_value)
                    else None
                ),
                target_initial=(
                    price + direction * policy.target_atr * atr_value
                    if (policy.target_atr and atr_value)
                    else None
                ),
                trail_initial=(price if (policy.trail_atr and atr_value) else None),
                bar_at=_iso(series.timestamps[index]),
                entry_bar_at=_iso(series.timestamps[index]),
                entry_bar_index=index,
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
        # The trailing stop is armed from the first completed bar *after* the one
        # the position opened on, and it is tested against the level this bar left
        # behind.  The old guard read ``index > position.bar_index``, but
        # ``_decide`` sets ``bar_index = index`` immediately before calling here, so
        # the comparison was ``index > index``: always false, and a trail could
        # never close anything -- before or after a restart.  The entry bar is now
        # compared by its own stamp, which survives a restart and a sliding window
        # (the series is rebuilt each cycle, so raw indices are not comparable
        # across cycles at all).
        armed = position.trail is not None and _after_entry_bar(
            position.entry_bar_at, _iso(series.timestamps[index])
        )
        hit_trail = bool(
            armed and (low <= position.trail if direction > 0 else high >= position.trail)
        )
        hit_target = position.target is not None and (
            high >= position.target if direction > 0 else low <= position.target
        )
        if hit_stop or hit_trail:
            level = float(position.stop if hit_stop else position.trail)  # type: ignore[arg-type]
            reason = EXIT_STOP if hit_stop else EXIT_TRAIL
            self._close(position, level, quote, reason, basis=BASIS_LEVEL)
            return True
        if hit_target and position.target is not None:
            self._close(position, float(position.target), quote, EXIT_TARGET, basis=BASIS_LEVEL)
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
            # What a money figure needs to mean anything.  Written here, at the
            # moment the fill is simulated, so the ledger can be read years later
            # without a terminal and without a guess.
            "quote_currency": position.quote_currency,
            "contract_size": position.contract_size,
            "quote_bid": _decimal(getattr(quote, "bid", None)),
            "quote_ask": _decimal(getattr(quote, "ask", None)),
            "quote_time": _iso(getattr(quote, "time", None)),
            "simulated": True,
            "priced_from": "BROKER_TICK",
        }

    def _close(
        self,
        position: Position,
        price: float,
        quote: Any,
        reason: str,
        *,
        basis: str = BASIS_BID_ASK,
    ) -> None:
        facts = self._facts(position.symbol)
        contract = _decimal(position.contract_size) or _decimal(
            getattr(facts, "contract_size", None)
        ) or 0.0
        point = _decimal(getattr(facts, "point", None)) or 0.0
        # A contract size that is not a positive number makes the money figure
        # meaningless.  The close still happens (a ledger must not be left
        # half-open) but the P&L is recorded as unknown, never as zero.
        priced = contract > 0
        gross = (
            position.direction * (price - position.entry_price) * contract * position.lots
            if priced
            else None
        )
        mid_move = (
            position.direction * ((float(quote.bid) + float(quote.ask)) / 2.0 - position.entry_mid)
            if point > 0
            else None
        )
        # What the two fill prices already contain, stated per leg rather than
        # claimed in general: a stopped-out exit fills at the level, so it carries
        # neither the spread nor the modelled slippage on that side.
        modelled = ["spread_open", "slippage_open"]
        if basis == BASIS_BID_ASK:
            modelled.extend(["spread_close", "slippage_close"])
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
            "entry_at": position.entry_at,
            "entry_price": round(position.entry_price, 8),
            "exit_price": round(price, 8),
            # Price P&L only: whatever the two fill prices contain (see
            # ``costs_modelled``), and nothing for commission or swap, which this
            # paper test does not simulate at all.  ``None`` means unpriceable --
            # never a zero standing in for an unknown.
            "simulated_net_quote": None if gross is None else round(gross, 4),
            "simulated_gross_quote": None if gross is None else round(gross, 4),
            "simulated_mid_move_pips": None if mid_move is None else round(mid_move / point, 1),
            "unpriced": "" if priced else "contract_size_unknown",
            "quote_currency": position.quote_currency,
            "contract_size": position.contract_size or float(contract),
            # The trailing state the exit was decided on, so why a trade closed on
            # its trail (and from which extreme) is read from the ledger, not inferred.
            "trail": None if position.trail is None else round(position.trail, 8),
            "trail_extreme": round(position.best, 8),
            "entry_bar_at": position.entry_bar_at,
            "exit_basis": basis,
            "costs_modelled": modelled,
            "costs_not_modelled": ["commission", "swap"],
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
        self._record(EVENT_CLOSE_RECORDED, {"key": position.key, "reason": reason})
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
            "version": STATE_VERSION,
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
            # ``state_view``, not ``audit_view``: a page and a restart need
            # different things, and resuming from a page summary is exactly how a
            # position came back with ``atr=0.0`` and no trail.
            "positions": [position.state_view() for position in self._positions.values()],
            "last_bar": dict(self._last_bar),
            "recovery": dict(self._recovery),
        }
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_name(self._state_path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError:
            self._reason = REASON_LEDGER

    def _restore_state(self) -> dict[str, Any]:
        """Resume open positions after a restart, exactly or not at all.

        The rule this method exists to keep: a resumed position must be managed
        by the *same* numbers the uninterrupted run would have used.  Three ways
        that can fail, all of them reported rather than papered over:

        * the state document is missing or unreadable (an empty folder is not a
          failure -- there is simply nothing to resume);
        * a record is missing a field the exit logic reads (the ATR of a trailing
          stop, an entry price, a direction, a lot size);
        * a record names a watch this run no longer follows, so nothing will ever
          manage it.

        When any of those happen the caller refuses to start and the state file is
        left untouched.  Nothing is defaulted, and no stop is invented.
        """

        self._positions = {}
        self._last_bar = {}
        report: dict[str, Any] = {
            "ok": True,
            "version": 0,
            "restored": 0,
            "migrated": 0,
            "failed": [],
            "detail": "",
        }
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._recovery = report  # nothing stored: the first run
            return report
        except OSError as error:
            report.update(
                ok=False,
                detail=(
                    "the paper state file exists but could not be read "
                    f"({type(error).__name__}); nothing will be resumed"
                ),
            )
            self._recovery = report
            return report
        try:
            payload = json.loads(raw)
        except ValueError as error:
            report.update(
                ok=False,
                detail=(
                    "the paper state file is not valid JSON "
                    f"({type(error).__name__}); nothing will be resumed, and the file "
                    "has been left as it was"
                ),
            )
            self._recovery = report
            return report
        if not isinstance(payload, Mapping):
            report.update(
                ok=False,
                detail="the paper state document is not an object; nothing will be resumed",
            )
            self._recovery = report
            return report

        version = payload.get("version")
        report["version"] = version if isinstance(version, int) else 0
        watches = {watch.key: watch for watch in self._watches}

        for entry in payload.get("positions") or ():
            if not isinstance(entry, Mapping):
                report["failed"].append(
                    {"key": "", "symbol": "", "reason": "position record is not an object"}
                )
                continue
            position, problem = self._position_from_state(entry, watches=watches)
            if position is None:
                report["failed"].append(
                    {
                        "key": str(entry.get("key") or ""),
                        "symbol": str(entry.get("symbol") or ""),
                        "reason": problem,
                    }
                )
                continue
            self._positions[position.key] = position
            report["restored"] += 1
            if position.migrated:
                report["migrated"] += 1
                self._record(
                    EVENT_MIGRATED,
                    {
                        "key": position.key,
                        "symbol": position.symbol,
                        "from_version": report["version"],
                        "derivation": position.migration,
                    },
                )
            self._record(
                EVENT_RESTORED,
                {
                    "key": position.key,
                    "symbol": position.symbol,
                    "atr": position.atr,
                    "bar_index": position.bar_index,
                    "stop": position.stop,
                    "target": position.target,
                    "trail": position.trail,
                    "best": position.best,
                    "migrated": position.migrated,
                },
            )

        # The bars already decided on.  Without this a restart re-decides the bar
        # it stopped on and can act on a signal the uninterrupted run had spent.
        stored_bars = payload.get("last_bar")
        if isinstance(stored_bars, Mapping):
            for key, stamp in stored_bars.items():
                if str(key) in watches and isinstance(stamp, str) and stamp:
                    self._last_bar[str(key)] = stamp

        if report["failed"]:
            report["ok"] = False
            report["detail"] = (
                f"{len(report['failed'])} stored paper position(s) cannot be resumed "
                "exactly; the run is blocked and the state file is untouched"
            )
            self._record(
                EVENT_RECOVERY_FAILED,
                {
                    "failed": len(report["failed"]),
                    "reasons": sorted({item["reason"] for item in report["failed"]})[:4],
                },
            )
        self._recovery = report
        return report

    def _position_from_state(
        self, entry: Mapping[str, Any], *, watches: Mapping[str, Watch]
    ) -> tuple[Position | None, str]:
        """One stored record, resumed exactly, migrated with a note, or refused."""

        key = str(entry.get("key") or "").strip()
        symbol = str(entry.get("symbol") or "").strip()
        if not key:
            return None, "the record has no position key"
        if not symbol:
            return None, "the record has no symbol"
        watch = watches.get(key)
        if watch is None:
            return None, (
                "no watched row matches this position, so nothing would manage it "
                "(it would sit open for ever)"
            )

        direction_text = str(entry.get("direction") or "").strip().upper()
        if direction_text not in {"LONG", "SHORT"}:
            return None, "the record's direction is not LONG or SHORT"
        direction = 1 if direction_text == "LONG" else -1

        lots = _state_number(entry.get("lots"))
        if lots is None or lots <= 0:
            return None, "the record's lot size is missing or not a positive number"
        entry_price = _state_number(entry.get("entry_price"))
        if entry_price is None or entry_price <= 0:
            return None, "the record's entry price is missing or not a positive number"
        entry_at = str(entry.get("entry_at") or "").strip()
        if not entry_at:
            return None, "the record has no entry time"

        recorded_version = entry.get("state_version")
        version = recorded_version if isinstance(recorded_version, int) else 0

        # -- the trailing stop's unit ---------------------------------------
        # ``_manage`` reads ``position.atr`` only when the position has a trail
        # *and* the candidate's policy trails by ATR.  Where it does, a missing
        # ATR changes the exit and cannot be derived: refuse.  Where it does not,
        # the value is never read, so it may be absent -- but the record says so.
        policy = watch.candidate.policy()
        trails_by_atr = bool(policy.trail_atr)
        trail = _state_number(entry.get("trail"))
        needs_atr = trail is not None and trails_by_atr
        atr = _state_number(entry.get("atr"))
        derived: list[str] = []
        if atr is None or atr <= 0:
            if needs_atr:
                return None, (
                    "the trailing stop is measured in an ATR this record does not "
                    "carry, so the next bar's trail cannot be computed"
                )
            atr = 0.0
            derived.append("atr: not recorded, and not read by this candidate's exit rules")

        stored_index = entry.get("bar_index")
        if isinstance(stored_index, bool) or not isinstance(stored_index, int) or stored_index < 0:
            bar_index = 0
            derived.append(
                "bar_index: not recorded; it is re-derived from the next completed bar, "
                "exactly as the previous build did on every decision"
            )
        else:
            bar_index = stored_index

        # -- the bar the trade opened on ------------------------------------
        # This is what arms the trailing stop (``_after_entry_bar``).  A record
        # that carries a live trail but no knowable entry bar could never exit on
        # its trail -- the exact defect this guard fix removes -- so it is refused
        # rather than resumed with a trail that is silently inert.
        entry_bar_at = str(entry.get("entry_bar_at") or "").strip()
        entry_bar_index = entry.get("entry_bar_index")
        if not isinstance(entry_bar_index, int) or isinstance(entry_bar_index, bool):
            entry_bar_index = -1
        if not entry_bar_at:
            # Schema 2 and earlier: ``bar_at`` is the last bar this position was
            # *managed* on, which is always at or after the entry bar, so using it
            # can only delay a trail trigger by one bar -- never advance one.
            entry_bar_at = str(entry.get("bar_at") or "").strip()
            if entry_bar_at:
                derived.append(
                    "entry_bar_at: not recorded; taken from the last managed bar, "
                    "which is never before the entry bar"
                )
        if not entry_bar_at:
            if trail is not None:
                return None, (
                    "the position carries a trailing stop but no bar the trade "
                    "opened on, so the trail could never be armed"
                )
            derived.append(
                "entry_bar_at: not recorded; there is no trailing stop to arm"
            )
        if entry_bar_index < 0:
            entry_bar_index = bar_index

        best = _state_number(entry.get("best"))
        if best is None:
            best = entry_price
            derived.append(
                "best: not recorded; it starts again at the entry price, and the trail "
                "itself is carried over unchanged"
            )

        stop = _state_number(entry.get("stop"))
        target = _state_number(entry.get("target"))
        stop_initial = _state_number(entry.get("stop_initial"))
        target_initial = _state_number(entry.get("target_initial"))
        trail_initial = _state_number(entry.get("trail_initial"))
        entry_mid = _state_number(entry.get("entry_mid"))
        notional = _state_number(entry.get("notional_quote"))
        contract = _state_number(entry.get("contract_size"))
        if entry_mid is None or entry_mid <= 0:
            entry_mid = entry_price
            derived.append(
                "entry_mid: not recorded; the mid at fill is not used by any exit rule"
            )

        position = Position(
            key=key,
            candidate_id=str(entry.get("candidate_id") or watch.candidate_id),
            symbol=symbol,
            timeframe=str(entry.get("timeframe") or watch.timeframe),
            direction=direction,
            lots=lots,
            entry_at=entry_at,
            entry_price=entry_price,
            entry_mid=entry_mid,
            atr=atr,
            stop=stop,
            target=target,
            trail=trail,
            best=best,
            notional_quote=0.0 if notional is None else notional,
            bar_index=bar_index,
            restored=True,
            quote_currency=str(entry.get("quote_currency") or "").upper(),
            contract_size=0.0 if contract is None else max(0.0, contract),
            stop_initial=stop if stop_initial is None else stop_initial,
            target_initial=target if target_initial is None else target_initial,
            trail_initial=trail if trail_initial is None else trail_initial,
            bar_at=str(entry.get("bar_at") or ""),
            entry_bar_at=entry_bar_at,
            entry_bar_index=entry_bar_index,
            migrated=bool(version < STATE_VERSION),
            migration="; ".join(derived),
        )
        return position, ""

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
        if not _finite_positive(point) or not _finite_positive(contract):
            return None
        model = cost_model_for(
            facts, account_currency=str(getattr(facts, "account_currency", "") or "")
        )
        return None if getattr(model, "unusable", False) else model

    def _fresh_quote(self, symbol: str) -> Any:
        """The current quote, and what this cycle saw of it.

        What is stored here is *evidence*, not a verdict: the quote's own
        timestamp, when it was read, and the prices.  Whether it is fresh is a
        question about the page's clock, so it is answered when the page is
        built (see :meth:`snapshot`), never frozen at read time.
        """

        try:
            quote = self._quotes.tick(symbol)
        except Exception:
            with self._lock:
                self._quotes_seen[symbol] = {
                    "checked_at": _iso(self._clock()),
                    "quote_time": None,
                    "state": "unavailable",
                    "bid": None,
                    "ask": None,
                }
            return None
        when = getattr(quote, "time", None)
        if isinstance(when, datetime):
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            quote_time = when
        else:
            quote_time = None
        age = (
            None
            if quote_time is None
            else max(0.0, (self._clock() - quote_time).total_seconds())
        )
        bid = _decimal(getattr(quote, "bid", None))
        ask = _decimal(getattr(quote, "ask", None))
        with self._lock:
            self._quotes_seen[symbol] = {
                # When the read happened, and the quote's own time.  Both are
                # absolute instants, so any later reader can work out the age
                # for itself instead of inheriting this cycle's answer.
                "checked_at": _iso(self._clock()),
                "quote_time": _iso(quote_time),
                "state": (
                    "fresh"
                    if (age is not None and age <= self._max_age)
                    else ("stale" if age is not None else "unknown_age")
                ),
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
            # Keyword arguments: the terminal's own signature is
            # ``rates(symbol, timeframe, *, start, end)``.
            rows = self._quotes.rates(watch.symbol, watch.timeframe, start=start, end=end)
        except Exception:
            return None
        if not rows:
            return None
        now = self._clock()
        rows = list(rows)
        # The terminal reports the bar that is still forming.  A decision may only
        # read *completed* bars, so the forming one is dropped, never half-used.
        if rows:
            last_stamp = rows[-1].get("timestamp") if isinstance(rows[-1], Mapping) else None
            if isinstance(last_stamp, datetime):
                if last_stamp.tzinfo is None:
                    last_stamp = last_stamp.replace(tzinfo=timezone.utc)
                if last_stamp + timedelta(seconds=seconds) > now:
                    rows = rows[:-1]
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

    def _note_latency(self, milliseconds: float) -> None:
        """Record one observed decision duration.  Never used to make a promise."""

        value = max(0.0, float(milliseconds))
        if self._latency_samples == 0:
            self._latency_ms["min"] = value
        self._latency_ms["last"] = value
        self._latency_ms["min"] = min(self._latency_ms["min"], value)
        self._latency_ms["max"] = max(self._latency_ms["max"], value)
        self._latency_ms["total"] += value
        self._latency_samples += 1

    def latency(self) -> dict[str, Any]:
        """What was actually measured, with its sample count and no promises."""

        samples = self._latency_samples
        mean = self._latency_ms["total"] / samples if samples else 0.0
        return {
            "unit": "milliseconds",
            "basis": "measured in this process on this machine",
            "samples": samples,
            "last": round(self._latency_ms["last"], 3),
            "mean": round(mean, 3),
            "min": round(self._latency_ms["min"], 3),
            "max": round(self._latency_ms["max"], 3),
            "note": (
                "Decision time only: from the completed bar to this cycle's decision, "
                "including the simulated fill when a signal fires. It says nothing "
                "about order submission, which this loop cannot do."
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        """Everything a page may show, with freshness judged *now*.

        Two rules this method exists to keep:

        * a quote's age and freshness are computed against the clock at the
          moment of reading, from the quote's own timestamp -- a page rendered
          ten minutes after the last tick says "ten minutes", not the verdict
          that was true when the tick arrived;
        * an entry fill *is* a fill.  ``orders``/``fills`` keep their historical
          names for the two ledger lists, and the explicit ``entry_fills`` /
          ``exit_fills`` / ``fills_total`` keys say the same thing without the
          ambiguity that once produced "5 orders, 0 filled".
        """

        now = self._clock()
        with self._lock:
            age_limit = self._max_age
            quotes = []
            for symbol, seen in sorted(self._quotes_seen.items()):
                quote_time = _parse_iso(seen.get("quote_time"))
                age = (
                    None
                    if quote_time is None
                    else max(0.0, (now - quote_time).total_seconds())
                )
                fresh = bool(age is not None and age <= age_limit)
                quotes.append(
                    {
                        "symbol": symbol,
                        "state": (
                            "fresh"
                            if fresh
                            else ("stale" if age is not None else "unavailable")
                        ),
                        "age_seconds": None if age is None else round(age, 1),
                        "fresh": fresh,
                        # Both absolute instants, so a reader never has to trust
                        # this cycle's arithmetic.
                        "quote_time": seen.get("quote_time") or "",
                        "checked_at": seen.get("checked_at") or "",
                        # The last quote the loop saw, so the page can show feed
                        # health and a mark.  Strings: the page never does maths on
                        # a price, and a Decimal in HTML is a rendering accident.
                        "bid": None if seen.get("bid") is None else str(seen.get("bid")),
                        "ask": None if seen.get("ask") is None else str(seen.get("ask")),
                    }
                )
            closed = [event for event in self._fills if event.get("kind") == EVENT_CLOSED]
            opened = [event for event in self._fills if event.get("kind") == EVENT_FILL]
            return {
                "status": self._status,
                "running": self.running,
                "state_version": STATE_VERSION,
                "recovery": dict(self._recovery),
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
                # One name per question.  An entry that opened a position is an
                # entry fill; a close is an exit fill; both are simulated fills.
                "entry_fills": opened[-NUMBER_OF_LEDGER_EVENTS:],
                "exit_fills": closed[-NUMBER_OF_LEDGER_EVENTS:],
                "fills_total": len(opened) + len(closed),
                "closed_trades": len(closed),
                # Historical keys, kept so a reader sees the same list under the
                # old name.  ``orders`` is the *entry* list and ``fills`` the
                # *exit* list; nothing may count one as the other's outcome.
                "orders": opened[-NUMBER_OF_LEDGER_EVENTS:],
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
                "latency": self.latency(),
                "confirmation": {
                    "enabled": self._confirmation is not None,
                    "vetoed": self._vetoed,
                    "can_only_veto": True,
                },
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
    "REASON_STATE_RECOVERY",
    "RUNNING",
    "STATE_VERSION",
    "STOPPED",
    "Watch",
    "summarise",
    "watches_from_rows",
]
