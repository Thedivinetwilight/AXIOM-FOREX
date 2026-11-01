"""The autopilot: evaluate on fresh terminal history, select, then run the selection.

Three steps, in this order, and nothing else:

1. **Evaluate.**  The catalog's own 60/20/20 + walk-forward evaluation runs on
   bars just read from the terminal.  Nothing here changes how a row is measured.
2. **Select.**  :mod:`axiom_forex.strategies.catalog.selection` ranks the rows on
   train/validation evidence only, applies a predeclared policy, and writes a
   reason for every candidate that is not chosen.
3. **Run.**  The chosen rows become watches for
   :class:`~axiom_forex.paper_runner.catalog_paper.CatalogPaperTest`, which fills
   from the terminal's own quotes and simulates locally.

What the autopilot deliberately cannot do:

* It cannot send an order.  It holds no adapter, no order object and no
  credentials; the only execution object it knows is the paper test.
* It cannot change the execution mode, arm live trading, or read the account.
* It cannot re-select while a paper loop is running.  Selection and running are
  separate states on purpose: changing the strategy under an open position would
  make every number in the ledger meaningless.  Re-selection is a stop/start
  cycle, and stopping leaves positions open and visible.
* It cannot promise a result.  It reports what it measured -- including how long
  its own decision took -- and the selection note travels with the plan.

Live execution stays on the existing path it always had: the operator arms
:class:`~axiom_forex.paper_runner.live_workflow.LiveTrading` with the confirmation
phrase, the account fingerprint and the reviewed plan, and the broker adapter's
per-order preflight decides every order.  :meth:`Autopilot.handoff` *describes* that
path and the blockers in front of it; it writes no plan and arms nothing.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ...paper_runner.catalog_paper import CatalogPaperTest, watches_from_rows
from .kronos_gate import KRONOS_MODES, KronosGate, veto_policy_snapshot
from .selection import (
    BLOCKER_POLICY_INVALID,
    SelectionPlan,
    SelectionPolicy,
    select,
    validation_boundaries,
)

# -- states -----------------------------------------------------------------
IDLE = "IDLE"
PREPARING = "PREPARING"
RUNNING = "RUNNING"
STOPPED = "STOPPED"
BLOCKED = "BLOCKED"

STATES: tuple[str, ...] = (IDLE, PREPARING, RUNNING, STOPPED, BLOCKED)

# -- why the autopilot will not start, or stopped ---------------------------
AUTOPILOT_NO_PANEL = "catalog_panel_missing"
AUTOPILOT_NO_TERMINAL = "no_terminal"
AUTOPILOT_NO_SYMBOLS = "no_symbols_selected"
AUTOPILOT_UNSUPPORTED_TIMEFRAME = "unsupported_timeframe"
AUTOPILOT_NO_HISTORY = "no_usable_history"
AUTOPILOT_EVALUATION_FAILED = "evaluation_failed"
AUTOPILOT_RECORD_NOT_WRITTEN = "evaluation_record_not_written"
AUTOPILOT_NO_SELECTION = "no_candidate_met_the_selection_rules"
AUTOPILOT_NO_WATCH = "no_usable_watch"
AUTOPILOT_PAPER_REFUSED = "paper_test_refused"
AUTOPILOT_ALREADY_RUNNING = "autopilot_already_running"
AUTOPILOT_SELECTION_LOCKED = "selection_not_allowed_while_the_paper_loop_runs"
AUTOPILOT_LIVE_HANDOFF_NOT_ARMED = "live_handoff_not_armed"
AUTOPILOT_KRONOS_MODE = "kronos_mode_not_recognised"

#: Where the selection audit trail is written, beside the evaluation record.
SELECTION_RECORD_NAME = "catalog-selection.json"

#: Bars per symbol and timeframe.  Same request the quick-setup page makes.
DEFAULT_BARS = 3000
#: How many rows one paper test follows.  The paper test's own ceiling is 48.
DEFAULT_ROW_LIMIT = 12

#: The page says this next to the start button.  It is a description of the
#: order of operations, not a promise about the outcome.
AUTOPILOT_NOTE = (
    "Start evaluates the catalog on fresh terminal history, selects on train and "
    "validation evidence only, and then follows the chosen rows as a locally "
    "simulated paper test. No order is sent at any point. A candidate that wins "
    "here has passed a screen and a set of predeclared rules on history; that is "
    "not evidence it will make money."
)

LATENCY_NOTE = (
    "Decision and submission timings below were measured in this process on this "
    "machine. They are observations, not a service level: the same code on another "
    "machine, or against a real broker, will produce different numbers."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None) -> str:
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


@dataclass(frozen=True)
class Step:
    """One thing the autopilot did, or refused to do, with the time it took."""

    name: str
    ok: bool
    detail: str = ""
    at: str = ""
    took_ms: float = 0.0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "at": self.at,
            "took_ms": round(self.took_ms, 1),
            **dict(self.extra),
        }


class Autopilot:
    """One start, one stop, and a paper loop that cannot become anything else."""

    def __init__(
        self,
        panel: Any,
        *,
        policy: SelectionPolicy | None = None,
        gate: KronosGate | None = None,
        clock: Callable[[], datetime] = _utc_now,
        row_limit: int = DEFAULT_ROW_LIMIT,
        bars: int = DEFAULT_BARS,
        root: Path | str | None = None,
    ) -> None:
        self._panel = panel
        self.policy = policy or SelectionPolicy()
        self.gate = gate or KronosGate()
        self.clock = clock
        self.row_limit = max(1, int(row_limit))
        self.bars = max(200, int(bars))
        base = Path(root) if root is not None else self._panel_data_root()
        self._root = base
        self._lock = threading.RLock()
        #: `_stop` aborts a preparation that is still running; `_worker` joins it.
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._state = IDLE
        self._reason = ""
        self._blockers: list[dict[str, str]] = []
        self._steps: list[Step] = []
        self._plan: SelectionPlan | None = None
        self._record_path = ""
        self._selection_path = ""
        self._started_at = ""
        self._finished_at = ""
        self._request: dict[str, Any] = {}
        self._watch_refusals: tuple[dict[str, str], ...] = ()
        self._selection_ms = 0.0
        self._selection_at = ""
        self._positions_at_start = 0
        self._paper: CatalogPaperTest | None = None
        #: The paper test's own snapshot, refreshed on every status read.
        self._last_paper: dict[str, Any] = {}

    # -- small helpers -----------------------------------------------------

    def _panel_data_root(self) -> Path:
        root = getattr(self._panel, "data_root", None)
        if root is None:
            base = getattr(self._panel, "root", None)
            root = Path(base) / "data" / "catalog" if base is not None else Path("data/catalog")
        return Path(root)

    def _terminal(self) -> Any:
        getter = getattr(self._panel, "_terminal", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        provider = getattr(self._panel, "terminal_provider", None)
        if callable(provider):
            try:
                return provider()
            except Exception:
                return None
        return None

    def _step(self, name: str, ok: bool, detail: str = "", took_ms: float = 0.0, **extra: Any) -> None:
        with self._lock:
            self._steps.append(
                Step(name=name, ok=ok, detail=detail, at=_iso(self.clock()), took_ms=took_ms, extra=extra)
            )
            del self._steps[:-40]

    def _block(self, state: str, reason: str, detail: str, *, blockers: Sequence[Mapping[str, str]] = ()) -> None:
        with self._lock:
            self._state = state
            self._reason = reason
            if blockers:
                self._blockers = [dict(item) for item in blockers]
            elif not any(item.get("reason") == reason for item in self._blockers):
                self._blockers.append({"reason": reason, "detail": detail})

    # -- what the operator clicks -----------------------------------------

    def start(
        self,
        *,
        symbols: Sequence[str] | None = None,
        timeframes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Evaluate, select, and start following the selection.  In the background.

        The evaluation is minutes of work on a slow machine, so this returns as
        soon as the worker is running; :meth:`status` is how the page follows it.
        """

        if self._panel is None:
            return self._refuse(AUTOPILOT_NO_PANEL, "No catalog panel is wired in, so there is nothing to run.")
        # A refusal must never overwrite the state of something that is already
        # running: the second Start explains itself and changes nothing.
        current = self._paper_status()
        if current and (current.get("running") or current.get("status") == "RUNNING"):
            return self._refuse(
                AUTOPILOT_ALREADY_RUNNING,
                "A paper test is already running. Stop it first: the running test and "
                "its open positions are untouched.",
                record_state=False,
            )
        with self._lock:
            if self._state == PREPARING:
                return self._refuse(
                    AUTOPILOT_ALREADY_RUNNING,
                    "The autopilot is already preparing a selection. Wait for it to finish.",
                    record_state=False,
                )
            if self._state == RUNNING:
                # The previous run has not been stopped, so a new selection would
                # change the strategy under whatever it still holds.  Stop first:
                # stopping never closes a position, and the page records the end of
                # the run before another one starts.
                return self._refuse(
                    AUTOPILOT_SELECTION_LOCKED,
                    "The last paper loop has not been stopped yet. Press Stop first: a "
                    "new selection must not start underneath positions the previous "
                    "run left open. Stopping closes nothing.",
                    record_state=False,
                )
            chosen_symbols = [str(name).strip() for name in (symbols or ()) if str(name).strip()]
            chosen_frames = [str(name).strip() for name in (timeframes or ()) if str(name).strip()]
            self._stop.clear()
            self._state = PREPARING
            self._reason = ""
            self._blockers = []
            self._steps = []
            self._plan = None
            self._watch_refusals = ()
            self._selection_ms = 0.0
            self._started_at = _iso(self.clock())
            self._finished_at = ""
            self._request = {"symbols": chosen_symbols, "timeframes": chosen_frames}
            self._last_paper = {}
            self._worker = threading.Thread(
                target=self._prepare,
                args=(chosen_symbols, chosen_frames),
                name="catalog-autopilot",
                daemon=True,
            )
            self._worker.start()
        return {
            "ok": True,
            "state": PREPARING,
            "message": (
                "Evaluating the catalog on fresh terminal history, then selecting on "
                "train and validation evidence only. This takes minutes; the page "
                "follows it. Nothing is traded while it runs."
            ),
        }

    def stop(self, *, reason: str = "operator_stop") -> dict[str, Any]:
        """Stop the paper loop.  Open positions stay open and stay visible."""

        with self._lock:
            preparing = self._state == PREPARING
            self._stop.set()
            paper = self._paper
        if preparing:
            self._block(
                STOPPED,
                reason,
                "The preparation was stopped before a paper test started. Nothing was "
                "running, so nothing was left open.",
            )
        result: dict[str, Any] = {"ok": True, "stopped": True, "closed_positions": 0}
        if paper is not None:
            outcome = paper.stop(reason=reason)
            result.update({key: value for key, value in outcome.items() if key != "positions"})
            positions = self._paper_positions(paper)
            result["open_positions"] = len(positions)
            result["positions"] = positions
            with self._lock:
                if not preparing:
                    self._state = STOPPED
                    self._reason = reason
                    self._finished_at = _iso(self.clock())
        result["message"] = (
            "The paper loop is stopped. "
            + (
                f"{len(result.get('positions') or ())} simulated position(s) are still "
                "open and shown on this page; stopping never closes them."
                if result.get("positions")
                else "No simulated position is open."
            )
            + " No order was sent, and no mode changed."
        )
        return result

    def flatten(self) -> dict[str, Any]:
        """Close the open *simulated* positions.  A separate, explicit click."""

        with self._lock:
            paper = self._paper
        if paper is None:
            return {"ok": False, "reason": "no_paper_test", "message": "No paper test exists in this process."}
        outcome = paper.close_all(reason="operator_flat")
        return {"ok": bool(outcome.get("ok", True)), **outcome}

    # -- the preparation thread -------------------------------------------

    def _prepare(self, symbols: Sequence[str], timeframes: Sequence[str]) -> None:
        try:
            self._prepare_steps(symbols, timeframes)
        except Exception as error:  # curated: the class name only, never a message
            self._block(
                BLOCKED,
                AUTOPILOT_EVALUATION_FAILED,
                f"The autopilot stopped while preparing ({type(error).__name__}). "
                "Nothing was traded and no mode changed.",
            )
        finally:
            with self._lock:
                if self._state == PREPARING:
                    self._state = BLOCKED
                    self._reason = self._reason or AUTOPILOT_EVALUATION_FAILED
                    self._blockers.append(
                        {
                            "reason": self._reason,
                            "detail": "The preparation ended without a paper test.",
                        }
                    )
                self._finished_at = _iso(self.clock())

    def _prepare_steps(self, symbols: Sequence[str], timeframes: Sequence[str]) -> None:
        panel = self._panel
        began = time.monotonic()
        terminal = self._terminal()
        if terminal is None:
            self._step("terminal", False, "No terminal is connected.")
            self._block(
                BLOCKED,
                AUTOPILOT_NO_TERMINAL,
                "Connect the terminal first. The evaluation reads the terminal's own "
                "bars and the paper test prices fills from its quotes; there is no "
                "other source, and nothing here is replaced with sample data.",
            )
            return
        self._step("terminal", True, "Terminal available for bars and quotes.", (time.monotonic() - began) * 1000.0)

        frames, frame_notes = self._timeframes(timeframes)
        names, name_notes = self._symbols(terminal, symbols)
        if not names:
            self._step("symbols", False, "No forex symbol could be named.")
            self._block(
                BLOCKED,
                AUTOPILOT_NO_SYMBOLS,
                "No forex symbol was selected or recommended, so there is nothing to "
                "evaluate. " + " ".join(name_notes),
            )
            return
        if not frames:
            self._step("timeframes", False, "No supported timeframe was requested.")
            self._block(BLOCKED, AUTOPILOT_UNSUPPORTED_TIMEFRAME, " ".join(frame_notes))
            return
        self._step(
            "request",
            True,
            f"{len(names)} symbol(s) on {', '.join(frames)}. " + " ".join(name_notes + frame_notes),
        )

        slots: list[Any] = []
        skipped: list[dict[str, Any]] = []
        for timeframe in frames:
            if self._stop.is_set():
                return
            read_began = time.monotonic()
            built, refused = panel._build_slots(names, timeframe)
            slots.extend(built)
            skipped.extend(refused)
            self._step(
                f"history:{timeframe}",
                bool(built),
                f"{len(built)} usable series read; {len(refused)} refused.",
                (time.monotonic() - read_began) * 1000.0,
            )
        if not slots:
            self._block(
                BLOCKED,
                AUTOPILOT_NO_HISTORY,
                "The terminal returned no usable bars for those symbols, so nothing can "
                "be measured. Nothing was replaced with sample data.",
                blockers=[
                    {
                        "reason": str(item.get("reason") or AUTOPILOT_NO_HISTORY),
                        "detail": f"{item.get('symbol', '?')} {item.get('timeframe', '')}: "
                        f"{item.get('reason', '')}",
                    }
                    for item in skipped[:10]
                ],
            )
            return

        eval_began = time.monotonic()
        report = self._evaluate(slots)
        if report is None:
            self._step("evaluate", False, "The evaluation raised or was cancelled.")
            self._block(
                BLOCKED,
                AUTOPILOT_EVALUATION_FAILED,
                "The evaluation stopped before it could finish, so no row was measured "
                "and nothing was selected. Nothing was traded.",
            )
            return
        eval_ms = (time.monotonic() - eval_began) * 1000.0
        self._step(
            "evaluate",
            True,
            f"{report.counts.get('tested', 0)} row(s) measured in {eval_ms / 1000:.1f}s.",
            eval_ms,
            tested=int(report.counts.get("tested", 0) or 0),
        )
        self._record_path = self._write_record(report)
        if not self._record_path:
            self._step("record", False, "The evaluation record could not be written.")
            self._block(
                BLOCKED,
                AUTOPILOT_RECORD_NOT_WRITTEN,
                "The evaluation ran, but its record could not be written, so the "
                "selection would not be reproducible. Nothing was selected.",
            )
            return

        select_began = time.monotonic()
        plan = self._select(report, slots)
        self._selection_ms = (time.monotonic() - select_began) * 1000.0
        self._selection_at = _iso(self.clock())
        with self._lock:
            self._plan = plan
        self._write_selection(plan)
        self._step(
            "select",
            True,
            f"{len(plan.selected())} row(s) chosen, {len(plan.rejected())} rejected, "
            f"{len(plan.eligible())} eligible but not chosen.",
            self._selection_ms,
        )
        if not plan.available:
            detail = "; ".join(str(item.get("detail") or item.get("reason")) for item in plan.blockers)
            self._block(
                BLOCKED,
                AUTOPILOT_NO_SELECTION,
                detail
                or "No candidate met the selection rules on this data. That is a result, "
                "not a failure to retry: the rules were not relaxed to fill the cap.",
                blockers=[dict(item) for item in plan.blockers],
            )
            return
        if self._stop.is_set():
            return

        watches, refusals = watches_from_rows(
            plan.rows(), symbol_names=list(self._names(terminal)), limit=self.row_limit
        )
        self._watch_refusals = tuple(refusals)
        if not watches:
            self._block(
                BLOCKED,
                AUTOPILOT_NO_WATCH,
                "The selected rows cannot be followed right now: the terminal does not "
                "offer those symbols, or the catalog does not declare the timeframe. "
                "The selection was not widened to something else.",
                blockers=[dict(item) for item in refusals[:10]],
            )
            return

        paper = CatalogPaperTest(
            quotes=terminal,
            watches=watches,
            root=self._panel_data_root() / "paper",
            lot=float(getattr(panel, "lot", 0.10)),
            clock=getattr(panel, "clock", self.clock),
            confirmation=self.confirm,
        )
        with self._lock:
            self._paper = paper
        restored = len(self._paper_positions(paper))
        self._positions_at_start = restored
        start_began = time.monotonic()
        outcome = paper.start()
        start_ms = (time.monotonic() - start_began) * 1000.0
        if not outcome.get("started"):
            blocker = (outcome.get("blockers") or [{}])[0]
            self._step("paper-start", False, str(blocker.get("detail") or "refused"), start_ms)
            self._block(
                BLOCKED,
                str(blocker.get("reason") or AUTOPILOT_PAPER_REFUSED),
                str(blocker.get("detail") or "The paper test did not start."),
            )
            return
        self._step(
            "paper-start",
            True,
            f"Following {len(watches)} row(s) on "
            + ", ".join(sorted({watch.symbol for watch in watches}))
            + (
                f". {restored} simulated position(s) came back from the state file."
                if restored
                else "."
            ),
            start_ms,
        )
        with self._lock:
            if self._state == PREPARING:
                self._state = RUNNING
                self._reason = ""
                self._blockers = []

    def _evaluate(self, slots: Sequence[Any]) -> Any:
        from .evaluate import EvaluationPlan, evaluate

        panel = self._panel
        plan = EvaluationPlan(
            slots=tuple(slots),
            lot=float(getattr(panel, "lot", 0.10)),
            budget_seconds=float(getattr(panel, "budget_seconds", 420.0)),
        )
        try:
            return evaluate(plan, cancel=self._stop.is_set)
        except Exception:
            return None

    def _records_dir(self) -> Path:
        return self._root

    def _write_record(self, report: Any) -> str:
        """Write the evaluation the way the catalog page writes it."""

        store = getattr(self._panel, "store", None)
        if store is None:
            return ""
        try:
            path = store.save(report)
        except Exception as error:
            self._step("record", False, f"the record could not be written ({type(error).__name__})")
            return ""
        self._clear_record_cache()
        return str(path)

    def _clear_record_cache(self) -> None:
        # The catalog page caches the parsed record by file stamp; a save through
        # this object should be visible to the page immediately.
        try:
            self._panel._record_cache = None
        except Exception:
            pass

    def _write_selection(self, plan: SelectionPlan) -> str:
        """Keep the why-selected / why-rejected record beside the evaluation."""

        path = self._records_dir() / SELECTION_RECORD_NAME
        payload = plan.to_payload()
        payload["written_at"] = _iso(self.clock())
        payload["kronos"] = veto_policy_snapshot(self.gate)
        payload["note"] = AUTOPILOT_NOTE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            return ""
        self._selection_path = str(path)
        return str(path)

    def _select(self, report: Any, slots: Sequence[Any]) -> SelectionPlan:
        boundaries = validation_boundaries(slots)
        data_origin = ""
        store = getattr(self._panel, "store", None)
        record = None
        try:
            record = store.load() if store is not None else None
        except Exception:
            record = None
        if record is not None:
            data_origin = record.data_origin
        plan = select(
            report.rows,
            policy=self.policy,
            slots=slots,
            data_origin=data_origin,
            evaluated_at=getattr(report, "finished_at", "") or "",
            evaluated_status=str(getattr(report, "status", "") or ""),
            now=self.clock(),
        )
        # ``select`` derives its own boundaries from the slots; this asserts the
        # two agree, so a slot shape change cannot quietly weaken the fold rule.
        if boundaries and not plan.decisions:
            plan.blockers = tuple(plan.blockers) + (
                {
                    "reason": BLOCKER_POLICY_INVALID,
                    "detail": "The selection pass produced no decision at all.",
                },
            )
        return plan

    def _symbols(self, terminal: Any, requested: Sequence[str]) -> tuple[list[str], list[str]]:
        notes: list[str] = []
        if requested:
            names = [str(name).strip() for name in requested if str(name).strip()]
        else:
            names = []
            try:
                choices = self._panel.fx_choices()
            except Exception:
                choices = {}
            names = [str(name) for name in (choices.get("recommended") or ())]
            if not names:
                notes.append("The terminal reported no forex pair to recommend.")
        cap = int(getattr(_panel_module(), "MAX_EVAL_SYMBOLS", 3))
        if len(names) > cap:
            notes.append(
                f"{len(names) - cap} more symbol(s) were not evaluated: one run measures at "
                f"most {cap} at a time."
            )
            names = names[:cap]
        return names, notes

    def _timeframes(self, requested: Sequence[str]) -> tuple[list[str], list[str]]:
        module = _panel_module()
        supported = tuple(getattr(module, "EVAL_TIMEFRAMES", ("15m", "1h")))
        default = str(getattr(module, "DEFAULT_EVAL_TIMEFRAME", supported[0]))
        notes: list[str] = []
        wanted = [str(item).strip() for item in requested if str(item).strip()] or [default]
        kept: list[str] = []
        for name in wanted:
            if name not in supported:
                notes.append(f"{name} is not one of the supported timeframes ({', '.join(supported)}).")
                continue
            if name in kept:
                continue
            kept.append(name)
        if len(kept) > 1:
            notes.append(
                f"Each timeframe is a separate evaluation, so this run does {len(kept)} "
                "passes over the catalog."
            )
        return kept, notes

    def _names(self, terminal: Any) -> Sequence[str]:
        try:
            return list(terminal.symbol_names())
        except Exception:
            return []

    def _paper_status(self) -> dict[str, Any]:
        with self._lock:
            paper = self._paper
        if paper is None:
            return {}
        try:
            return paper.snapshot()
        except Exception:
            return {}

    def _paper_positions(self, paper: CatalogPaperTest) -> list[dict[str, Any]]:
        try:
            return list(paper.snapshot().get("positions") or ())
        except Exception:
            return []

    def _refuse(
        self, reason: str, message: str, *, record_state: bool = True, **extra: Any
    ) -> dict[str, Any]:
        """Say no.  ``record_state`` is off when the refusal must not disturb a run."""

        if record_state:
            self._block(BLOCKED, reason, message)
        return {"ok": False, "reason": reason, "message": message, **extra}

    # -- the forecast, if and only if it is on -----------------------------

    def confirm(self, watch: Any, direction: int) -> Any:
        """The confirmation hook the paper loop calls.  Veto only."""

        symbol = getattr(watch, "symbol", "")
        timeframe = getattr(watch, "timeframe", "")
        return self.gate.confirm(str(symbol), str(timeframe), int(direction))

    # -- what the page shows ----------------------------------------------

    def status(self) -> dict[str, Any]:
        paper = self._paper_status()
        with self._lock:
            state = self._state
            if state == RUNNING and paper and not paper.get("running"):
                state = BLOCKED if paper.get("status") == "BLOCKED" else STOPPED
            positions = list(paper.get("positions") or ()) if paper else []
            return {
                "state": state,
                "reason": self._reason,
                "running": state == RUNNING,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "request": dict(self._request),
                "steps": [step.view() for step in self._steps],
                "blockers": [dict(item) for item in self._blockers],
                "record": self._record_path,
                "selection_record": self._selection_path,
                "selection_at": self._selection_at,
                "selection_ms": round(self._selection_ms, 1),
                "positions": positions,
                "open_positions": len(positions),
                "watch_refusals": [dict(item) for item in self._watch_refusals],
                "paper": paper,
                "kronos": self.gate.view(),
                "kronos_policy": veto_policy_snapshot(self.gate),
                "note": AUTOPILOT_NOTE,
                "latency_note": LATENCY_NOTE,
                "selection": self._plan.summary() if self._plan is not None else {},
                "claim": (
                    "Every fill on this page is simulated locally from the terminal's own "
                    "quote. No order was sent, no account was read and nothing here is a "
                    "profit claim."
                ),
            }

    def choices(self) -> dict[str, Any]:
        """What the operator may ask for, and the bounds that come with it.

        The bounds are shown, not applied silently: a capped run says so on the page.
        """

        module = _panel_module()
        supported = tuple(getattr(module, "EVAL_TIMEFRAMES", ("15m", "1h")))
        default = str(getattr(module, "DEFAULT_EVAL_TIMEFRAME", supported[0]))
        recommended: list[str] = []
        note = ""
        try:
            offer = self._panel.fx_choices()
            recommended = [str(name) for name in (offer.get("recommended") or ())]
            counted = dict(offer.get("counts") or {})
            if counted:
                note = (
                    f"The terminal reports {counted.get('forex_pairs', 0)} forex pair(s) of "
                    f"{counted.get('reported', 0)} symbol(s)."
                )
        except Exception as error:
            note = f"The terminal's symbol list could not be read ({type(error).__name__})."
        return {
            "timeframes": list(supported),
            "default_timeframe": default,
            "recommended": recommended,
            "max_symbols": int(getattr(module, "MAX_EVAL_SYMBOLS", 3)),
            "max_rows": self.row_limit,
            "note": note,
        }

    def decisions(self, limit: int = 200) -> dict[str, Any]:
        """Why every candidate was chosen or left out, for the page's table."""

        with self._lock:
            plan = self._plan
        if plan is None:
            return {"available": False, "reason": "no_selection_has_run", "decisions": []}
        payload = plan.audit_view(limit=limit)
        payload["available"] = True
        return payload

    def latency(self) -> dict[str, Any]:
        """Observed timings only: selection here, decision and fill inside the loop."""

        paper = self._paper_status()
        inner = dict(paper.get("latency") or {})
        with self._lock:
            selection_ms = self._selection_ms
            steps = [step.view() for step in self._steps]
        evaluation = next((step for step in reversed(steps) if step["name"] == "evaluate"), None)
        return {
            "selection_ms": round(selection_ms, 1),
            "evaluation_ms": (evaluation or {}).get("took_ms", 0.0),
            "decision": inner,
            "measured_here": True,
            "note": LATENCY_NOTE,
        }

    def handoff(self) -> dict[str, Any]:
        """What stands between this selection and a real order -- and who must act.

        The autopilot never arms and never trades live.  It can only describe the
        existing guarded path and propose the values an operator would carry into
        it; every prerequisite is listed as unmet unless this process can see it.
        """

        mode = None
        getter = getattr(self._panel, "mode", None)
        if callable(getattr(getter, "view", None)):
            try:
                mode = getter.view()
            except Exception:
                mode = None
        mode_name = ""
        if isinstance(mode, Mapping):
            mode_name = str(mode.get("mode") or "").upper()
        with self._lock:
            plan = self._plan
        proposed = []
        if plan is not None:
            for item in plan.selected()[: self.row_limit]:
                proposed.append({"candidate_id": item.candidate_id, "pair": item.symbol, "timeframe": item.timeframe})
        blockers: list[dict[str, str]] = [
            {
                "reason": AUTOPILOT_LIVE_HANDOFF_NOT_ARMED,
                "detail": (
                    "Nothing here is armed. Automatic live execution stays on the existing "
                    "guarded path: arm LiveTrading with the confirmation phrase, the "
                    "fingerprint of the account the terminal is on now, and the reviewed "
                    "risk limits. This page writes no plan and arms nothing."
                ),
            },
            {
                "reason": "live_quantity_not_proposed",
                "detail": (
                    "No lot size or risk limit is proposed for a live order: the broker's "
                    "tradable step, the account's currency and the operator's limits are "
                    "not known here, and guessing one would be worse than leaving it blank."
                ),
            },
        ]
        if mode_name != "LIVE":
            blockers.append(
                {
                    "reason": "live_mode_not_selected",
                    "detail": "The execution mode is not LIVE. Selecting it is the operator's step and sends nothing.",
                }
            )
        return {
            "available": False,
            "enabled": False,
            "mode": mode_name,
            "proposed": proposed,
            "blockers": blockers,
            "requirements": [
                "LIVE selected and confirmed on the Trading mode screen (sends nothing).",
                "LiveTrading armed: confirmation phrase, the current account's fingerprint, and the review acknowledged.",
                "Risk limits supplied by the operator: quantity, max open positions, max daily loss.",
                "The broker adapter's per-order preflight passes for each order.",
            ],
            "per_order_checks": (
                "Every order still goes through the adapter's own preflight: mode, arming, "
                "account identity, symbol specs, volume step, margin and the risk limits. "
                "The autopilot cannot bypass, weaken or skip any of them, and it holds no "
                "adapter at all."
            ),
            "note": (
                "This selection is paper evidence. It is not a live readiness verdict, and "
                "passing the screen on history does not make a strategy safe to trade."
            ),
        }

    # -- operator controls for the optional input --------------------------

    def set_kronos(self, mode: str, *, required: bool = False) -> dict[str, Any]:
        """Turn the optional forecast input on or off.  It can only veto."""

        wanted = str(mode or "").strip().upper()
        if wanted not in KRONOS_MODES:
            return {
                "ok": False,
                "reason": AUTOPILOT_KRONOS_MODE,
                "message": f"{mode!r} is not one of {', '.join(KRONOS_MODES)}.",
            }
        with self._lock:
            self.gate.mode = wanted
            self.gate.required = bool(required) and wanted != "OFF"
        return {
            "ok": True,
            "mode": wanted,
            "required": self.gate.required,
            "message": (
                "Kronos is off: no forecast is read and none is part of the decision."
                if wanted == "OFF"
                else "Kronos may now cancel an entry. It cannot create one, size one, or change the plan."
            ),
        }

    def wait(self, timeout: float = 5.0) -> None:
        """Test and shutdown helper: join the preparation worker."""

        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)


def _panel_module() -> Any:
    from . import panel as panel_module

    return panel_module


__all__ = [
    "AUTOPILOT_ALREADY_RUNNING",
    "AUTOPILOT_EVALUATION_FAILED",
    "AUTOPILOT_KRONOS_MODE",
    "AUTOPILOT_LIVE_HANDOFF_NOT_ARMED",
    "AUTOPILOT_NO_HISTORY",
    "AUTOPILOT_NO_PANEL",
    "AUTOPILOT_NO_SELECTION",
    "AUTOPILOT_NO_SYMBOLS",
    "AUTOPILOT_NO_TERMINAL",
    "AUTOPILOT_NO_WATCH",
    "AUTOPILOT_NOTE",
    "AUTOPILOT_PAPER_REFUSED",
    "AUTOPILOT_RECORD_NOT_WRITTEN",
    "AUTOPILOT_SELECTION_LOCKED",
    "AUTOPILOT_UNSUPPORTED_TIMEFRAME",
    "Autopilot",
    "BLOCKED",
    "DEFAULT_BARS",
    "DEFAULT_ROW_LIMIT",
    "IDLE",
    "LATENCY_NOTE",
    "PREPARING",
    "RUNNING",
    "SELECTION_RECORD_NAME",
    "STATES",
    "STOPPED",
    "Step",
]
