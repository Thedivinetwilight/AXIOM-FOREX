"""The page state for the Monday test: the catalog, its evaluation, and the run.

Three questions this panel answers, in the order the operator asks them:

1. **Which symbols are actually forex?**  The terminal's own reported legs decide;
   the seven majors are offered first, everything else stays selectable, and a
   metal or a stock CFD is excluded with a reason rather than silently dropped.
2. **What did the catalog measure?**  One button runs the declared candidates on
   the history the terminal can supply, writes the report to a local record, and
   shows tested / skipped / failed with reasons -- and it says plainly that a
   screen is not a profit claim.
3. **Can the paper test start?**  Only from a stored evaluation with a screened
   paper set, only with fresh terminal quotes, and only one at a time.  The run is
   live-market *paper*: real broker quotes, locally simulated fills, a ledger on
   disk, and no order surface anywhere in the graph.

Nothing here can connect a terminal, change an execution mode, arm live trading,
size a real position, or reach an order API.  The panel is handed a read-only
terminal and a bar provider; both are data.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import dataclasses

from axiom_forex.config.fx_symbols import MAJOR_PAIRS, forex_symbols, recommended
from axiom_forex.config.terminal_facts import SymbolFacts, symbol_facts
from axiom_forex.paper_runner.catalog_paper import (
    CatalogPaperTest,
    summarise,
    watches_from_rows,
)
from axiom_forex.research.engine import CostModel, cost_model_for
from axiom_forex.research.series import build_series, rows_from_rates

from .candidates import catalog, summary_counts
from .evaluate import EvaluationPlan, EvaluationReport, SeriesSlot, evaluate
from .store import RECORD_NAME, CatalogStore, summarise_paper_set

#: Actions this panel accepts.  They are also in the workstation's POST_ACTIONS.
ACTION_EVALUATE = "catalog-evaluate"
ACTION_PAPER_START = "catalog-paper-start"
ACTION_PAPER_STOP = "catalog-paper-stop"
ACTION_PAPER_FLAT = "catalog-paper-flat"
ACTIONS: frozenset[str] = frozenset(
    {ACTION_EVALUATE, ACTION_PAPER_START, ACTION_PAPER_STOP, ACTION_PAPER_FLAT}
)

#: Timeframes the evaluation may use.  Short enough to finish in a browser-sized
#: budget, long enough that a year of bars is a real sample.
EVAL_TIMEFRAMES: tuple[str, ...] = ("15m", "1h")
DEFAULT_EVAL_TIMEFRAME = "1h"

#: Bars per symbol and timeframe.  A year of 1h bars is about 6.2k; asking for a
#: round three thousand keeps the request quick and still spans months.
DEFAULT_BARS = 3000

#: Lot size for the research simulation.  One tenth of a lot: a size a paper test
#: can follow without pretending anything about the operator's risk.
DEFAULT_LOT = 0.10

#: How long a stored evaluation may seed a paper test before it must be rerun.
MAX_RECORD_AGE_SECONDS = 14 * 24 * 3600.0

#: A polite cap: the page has to stay a page.
MAX_EVAL_SYMBOLS = 3
MAX_EVAL_SECONDS = 420.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _EvaluationRun:
    """One background evaluation, with a status a page can read."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.started_at: str = ""
        self.finished_at: str = ""
        self.reason: str = ""
        self.message: str = ""
        self.cancelled = threading.Event()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


def _facts_from_terminal(terminal: Any, limit: int = 200) -> tuple[SymbolFacts, ...]:
    """Read the terminal's symbol facts, read-only, and never invent one."""

    if terminal is None:
        return ()
    try:
        names = list(terminal.symbol_names())[:limit]
    except Exception:
        return ()
    facts: list[SymbolFacts] = []
    for name in names:
        captured = _symbol_facts(terminal, name)
        if captured is not None:
            facts.append(captured)
    return tuple(facts)


def _symbol_facts(terminal: Any, symbol: str) -> SymbolFacts | None:
    """One symbol's facts, read from the terminal.  A missing name is filled in
    from the request; nothing else is ever filled in."""

    if terminal is None:
        return None
    try:
        rows = terminal.symbol_info(symbol)
    except Exception:
        return None
    for entry in rows if isinstance(rows, (list, tuple)) else [rows]:
        captured = symbol_facts(entry)
        if captured is None:
            continue
        if not captured.broker_symbol:
            captured = dataclasses.replace(captured, broker_symbol=symbol)
        return captured
    return None


@dataclass
class CatalogPanel:
    """The catalog's page state: symbols, evaluation, and the live paper test."""

    root: Path
    terminal_provider: Callable[[], Any] = lambda: None
    bars_provider: Callable[[str, str, int], Any] | None = None
    budget_seconds: float = MAX_EVAL_SECONDS
    lot: float = DEFAULT_LOT
    clock: Callable[[], datetime] = _utc_now
    max_record_age_seconds: float = MAX_RECORD_AGE_SECONDS

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.data_root = self.root / "data" / "catalog"
        self.store = CatalogStore(self.data_root)
        self._run = _EvaluationRun()
        self._report: EvaluationReport | None = None
        self._paper: CatalogPaperTest | None = None
        self._paper_watches: tuple[Any, ...] = ()
        self._lock = threading.RLock()
        self._last_outcome: dict[str, Any] = {}
        #: The last history check, with the instant it was made: a page render
        #: reuses it for a few seconds instead of re-reading the terminal.
        self._data_check: tuple[tuple[tuple[str, ...], str], datetime, dict[str, Any]] | None = None
        #: The evaluation record is a few megabytes once a thousand rows are in it,
        #: and every page render asks for it twice.  One parse per file change.
        self._record_cache: tuple[tuple[int, int], Any] | None = None

    # -- what the page shows ----------------------------------------------

    def fx_choices(self) -> dict[str, Any]:
        terminal = self._terminal()
        facts = _facts_from_terminal(terminal)
        if not facts:
            return {
                "available": False,
                "reason": "no_terminal_or_no_symbols",
                "message": (
                    "Connect the terminal first: the symbol offer comes from what the "
                    "terminal reports, not from a list in this repository."
                ),
                "majors": list(MAJOR_PAIRS),
                "choices": [],
                "excluded": [],
                "recommended": [],
            }
        choices = forex_symbols(facts)
        excluded = [
            item.audit_view()
            for item in (_classify_all(facts))
            if item.kind != "FOREX_PAIR"
        ]
        return {
            "available": True,
            "counts": {
                "reported": len(facts),
                "forex_pairs": len(choices),
                "usable": sum(1 for item in choices if item.usable),
                "excluded": len(excluded),
            },
            "majors": list(MAJOR_PAIRS),
            "choices": [item.audit_view() for item in choices[:60]],
            "excluded": excluded[:20],
            "recommended": list(recommended(facts)),
            "timeframes": list(EVAL_TIMEFRAMES),
            "default_timeframe": DEFAULT_EVAL_TIMEFRAME,
            "default_bars": DEFAULT_BARS,
        }

    def data_check(
        self,
        *,
        symbols: Sequence[str],
        timeframe: str = DEFAULT_EVAL_TIMEFRAME,
        ttl_seconds: float = 20.0,
    ) -> dict[str, Any]:
        """Read the terminal's own bars and say whether they can be evaluated.

        This is the check the guided start page shows as step 3: it asks the same
        bar reader the evaluation uses, builds the same series, and reports per
        symbol how many bars arrived, when the last one is, and -- when it cannot
        be used -- the reason.  Nothing is written, nothing is substituted, and a
        symbol the terminal cannot supply is never filled with sample data.

        The read is bounded (``MAX_EVAL_SYMBOLS`` symbols) and cached for a few
        seconds, so a page render cannot turn into a terminal poll.
        """

        names = [str(name).strip() for name in symbols if str(name).strip()][:MAX_EVAL_SYMBOLS]
        chosen = timeframe if timeframe in EVAL_TIMEFRAMES else DEFAULT_EVAL_TIMEFRAME
        now = self.clock()
        key = (tuple(names), chosen)
        with self._lock:
            cached = self._data_check
            if cached is not None and cached[0] == key:
                age = (now - cached[1]).total_seconds()
                if 0 <= age < ttl_seconds:
                    return dict(cached[2])
        view: dict[str, Any] = {
            "available": True,
            "timeframe": chosen,
            "checked_at": now.isoformat(),
            "bars_requested": DEFAULT_BARS,
            "requested": len(names),
            "rows": [],
            "usable": 0,
            "reason": "",
            "message": "",
        }
        if self._terminal() is None:
            view.update(
                available=False,
                reason="no_terminal",
                message=(
                    "Connect the terminal first: the bars come from its own rate "
                    "history, and there is no other price source."
                ),
            )
        elif not names:
            view.update(
                reason="no_symbols_selected",
                message="Choose the forex symbols to use before checking history.",
            )
        else:
            slots, skipped = self._build_slots(names, chosen)
            rows: list[dict[str, Any]] = []
            for slot in slots:
                stamps = list(getattr(slot.series, "timestamps", ()) or ())
                last = stamps[-1] if stamps else None
                age_seconds = None
                if last is not None:
                    try:
                        age_seconds = max(0.0, (now - last).total_seconds())
                    except TypeError:
                        age_seconds = None
                rows.append(
                    {
                        "symbol": slot.symbol,
                        "timeframe": slot.timeframe,
                        "usable": True,
                        "bars": len(slot.series),
                        "first": stamps[0].isoformat() if stamps else "",
                        "last": last.isoformat() if last is not None else "",
                        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                        "origin": slot.origin,
                        "reason": "",
                    }
                )
            for item in skipped:
                rows.append(
                    {
                        "symbol": str(item.get("symbol") or ""),
                        "timeframe": str(item.get("timeframe") or chosen),
                        "usable": False,
                        "bars": 0,
                        "first": "",
                        "last": "",
                        "age_seconds": None,
                        "origin": "",
                        "reason": str(item.get("reason") or "no usable history"),
                    }
                )
            view["rows"] = rows
            view["usable"] = len(slots)
            view["message"] = (
                f"{len(slots)} of {len(names)} symbol(s) returned usable "
                f"{chosen} history."
                if slots
                else "No selected symbol returned usable history."
            )
            if not slots:
                view["reason"] = "no_usable_history"
        if view["available"]:
            # Only a read that actually reached the terminal is cached: after a
            # connect the page must not keep saying "no terminal" for a few more
            # seconds.
            with self._lock:
                self._data_check = (key, now, dict(view))
        return view

    def _record(self) -> Any:
        """The stored evaluation, parsed once per file change."""

        path = self.store.path
        try:
            stat = path.stat()
        except OSError:
            self._record_cache = None
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._record_cache is not None and self._record_cache[0] == stamp:
            return self._record_cache[1]
        loaded = self.store.load()
        self._record_cache = (stamp, loaded)
        return loaded

    def evaluation_view(self) -> dict[str, Any]:
        record = self._record()
        counts = summary_counts()
        view: dict[str, Any] = {
            "catalog": counts,
            "running": self._run.running,
            "started_at": self._run.started_at,
            "finished_at": self._run.finished_at,
            "message": self._run.message,
            "record_path": str(self.store.path),
            "record_name": RECORD_NAME,
            "on_record": False,
            "paper_set": [],
            "paper_set_summary": "none",
            "blockers": [],
            "claim": "",
        }
        if record is not None:
            view["on_record"] = True
            view["status"] = record.status
            view["written_at"] = record.written_at
            view["counts"] = record.counts
            view["data_origin"] = record.data_origin
            view["paper_set"] = [dict(item) for item in record.paper_set][:20]
            view["paper_set_summary"] = summarise_paper_set(record.paper_set)
            view["claim"] = str(record.payload.get("claim") or "")
            view["blockers"] = [
                dict(blocker)
                for blocker in record.blockers(max_age_seconds=self.max_record_age_seconds)
            ]
        else:
            view["blockers"] = list(
                self.store.blockers(max_age_seconds=self.max_record_age_seconds)
            )
        if self._report is not None:
            view["last_run"] = self._report.summary()
            view["table"] = list(self._report.table(8))
        return view

    def paper_view(self) -> dict[str, Any]:
        with self._lock:
            paper = self._paper
            watches = self._paper_watches
        snapshot = paper.snapshot() if paper is not None else {}
        record = self._record()
        blockers = (
            list(paper.blockers())
            if paper is not None
            else [
                {
                    "reason": "paper_test_not_built",
                    "detail": "Build the test from a stored evaluation first.",
                }
            ]
        )
        if record is not None:
            blockers = blockers + [
                dict(item) for item in record.blockers(max_age_seconds=self.max_record_age_seconds)
            ]
        # While it is running, "already running" is not a blocker; the status says so.
        blockers = [
            item for item in blockers if item.get("reason") != "paper_test_already_running"
        ]
        return {
            "available": paper is not None,
            "built": paper is not None,
            "running": bool(snapshot.get("running")),
            "headline": summarise(snapshot) if snapshot else "not started",
            "snapshot": snapshot,
            "watches": [watch.audit_view() for watch in watches],
            "blockers": blockers,
            "record_status": record.status if record is not None else "",
            "record_origin": record.data_origin if record is not None else "",
        }

    def view(self) -> dict[str, Any]:
        return {
            "available": True,
            "symbols": self.fx_choices(),
            "evaluation": self.evaluation_view(),
            "paper": self.paper_view(),
            "claim": (
                "The catalog is a declared search on historical bars. A candidate that "
                "passes the screen is a short-list entry, not a profitable strategy and "
                "not a licence to trade."
            ),
        }

    # -- what a click does ------------------------------------------------

    def act(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if action == ACTION_EVALUATE:
            return self._evaluate(payload)
        if action == ACTION_PAPER_START:
            return self._paper_start(payload)
        if action == ACTION_PAPER_STOP:
            return self._paper_stop()
        if action == ACTION_PAPER_FLAT:
            return self._paper_flat()
        return self._refuse("unknown_action", "That action is not part of this panel.")

    # -- evaluation --------------------------------------------------------

    def _evaluate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._run.running:
                return self._refuse(
                    "evaluation_already_running",
                    "An evaluation is already running. Wait for it to finish.",
                )
        terminal = self._terminal()
        if terminal is None:
            return self._refuse(
                "no_terminal",
                "Connect the terminal first. The evaluation reads the terminal's own "
                "historical bars; without them there is nothing to measure.",
            )
        requested = _as_list(payload.get("symbols")) or list(self.fx_choices().get("recommended") or ())
        timeframe = str(payload.get("timeframe") or DEFAULT_EVAL_TIMEFRAME).strip()
        if timeframe not in EVAL_TIMEFRAMES:
            return self._refuse(
                "unsupported_timeframe",
                f"This page evaluates on {', '.join(EVAL_TIMEFRAMES)} only.",
            )
        symbols = [name for name in requested if name][:MAX_EVAL_SYMBOLS]
        if not symbols:
            return self._refuse(
                "no_symbols_selected",
                "Choose at least one forex symbol to evaluate.",
            )
        facts = _symbol_facts(terminal, symbols[0])
        if facts is None:
            return self._refuse(
                "symbol_facts_unavailable",
                "The terminal did not report specs for those symbols, so no cost model "
                "can be built and nothing is measured without one.",
            )
        slots, skipped = self._build_slots(symbols, timeframe)
        if not slots:
            return self._refuse(
                "no_usable_history",
                "The terminal returned no usable bars for those symbols, so there is "
                "nothing to evaluate. Nothing was replaced with sample data.",
                rows=skipped,
            )
        with self._lock:
            self._run = _EvaluationRun()
            self._run.started_at = self.clock().isoformat()
            run = self._run
            self._run.thread = threading.Thread(
                target=self._run_evaluation,
                args=(slots, run),
                name="catalog-evaluation",
                daemon=True,
            )
            self._run.thread.start()
        return self._ok(
            "Evaluation started on "
            + ", ".join(slot.symbol for slot in slots)
            + f" ({timeframe}). It runs in the background; reload this page for the result.",
            path="/quick-setup",
        )

    def _run_evaluation(self, slots: Sequence[SeriesSlot], run: _EvaluationRun) -> None:
        try:
            report = evaluate(
                EvaluationPlan(slots=tuple(slots), lot=self.lot, budget_seconds=self.budget_seconds),
                cancel=run.cancelled.is_set,
            )
        except Exception as error:  # curated: the message never reaches the page
            with self._lock:
                run.message = (
                    "The evaluation stopped before it could finish "
                    f"({type(error).__name__}). Nothing was traded and no mode changed."
                )
                run.finished_at = self.clock().isoformat()
                run.reason = "evaluation_failed"
            return
        try:
            self.store.save(report)
            self._record_cache = None
        except OSError:
            with self._lock:
                run.reason = "record_not_written"
                run.message = (
                    "The evaluation ran, but its record could not be written, so the "
                    "paper test cannot start from it."
                )
        with self._lock:
            self._report = report
            run.finished_at = self.clock().isoformat()
            run.reason = ""
            run.message = (
                f"Evaluated {report.counts['tested']} row(s) on "
                f"{len(slots)} series: {report.counts['screen']['PASSED_SCREEN']} passed the "
                f"screen, {report.counts['skipped']} skipped, {report.counts['failed']} failed."
            )

    def _build_slots(
        self, symbols: Sequence[str], timeframe: str
    ) -> tuple[tuple[SeriesSlot, ...], list[dict[str, Any]]]:
        slots: list[SeriesSlot] = []
        skipped: list[dict[str, Any]] = []
        if self.bars_provider is None:
            return (), [
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "usable": False,
                    "reason": "no_bar_provider",
                }
                for symbol in symbols
            ]
        terminal = self._terminal()
        seconds = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}.get(timeframe, 0)
        for symbol in symbols:
            facts = _symbol_facts(terminal, symbol)
            cost = self._cost(facts)
            if cost is None or not seconds:
                skipped.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "usable": False,
                        "reason": "no_cost_model" if cost is None else "unsupported_timeframe",
                    }
                )
                continue
            try:
                rows = self.bars_provider(symbol, timeframe, DEFAULT_BARS)
            except Exception as error:
                skipped.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "usable": False,
                        "reason": str(getattr(error, "reason", "") or type(error).__name__),
                    }
                )
                continue
            if not rows:
                skipped.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "usable": False,
                        "reason": "no_bars_returned",
                    }
                )
                continue
            series = build_series(
                symbol,
                str(getattr(facts, "pair", "") or symbol),
                timeframe,
                seconds,
                _point(facts),
                rows_from_rates(rows),
            )
            if len(series) < 200:
                skipped.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "usable": False,
                        "reason": f"only {len(series)} usable bar(s) returned",
                    }
                )
                continue
            slots.append(
                SeriesSlot(
                    symbol=symbol,
                    timeframe=timeframe,
                    series=series,
                    cost=cost,
                    origin=_origin_for(series),
                )
            )
        return tuple(slots), skipped

    def _cost(self, facts: SymbolFacts | None) -> CostModel | None:
        if facts is None:
            return None
        point = facts.point
        contract = facts.contract_size
        if point is None or contract is None or float(point) <= 0 or float(contract) <= 0:
            return None
        # The account's currency is not part of a symbol record; leaving it out
        # keeps every account-currency cost labelled as unconvertible instead of
        # guessed, which is exactly what the report should say.
        return cost_model_for(facts, account_currency="")

    # -- the paper test ----------------------------------------------------

    def build_paper_test(
        self, *, limit: int = 12, keys: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Build the live-market paper test from the stored evaluation.

        ``keys`` narrows the test to the rows the operator ticked on the start
        page.  An empty or absent selection means every screened row, which is
        what the older page has always done; a selection that matches nothing is
        refused rather than quietly widened back to everything.
        """

        record = self._record()
        if record is None:
            return self._refuse(
                "no_evaluation_on_record",
                "Run the catalog evaluation first: the paper test starts from what was "
                "measured, never from an idea nobody checked.",
            )
        blockers = record.blockers(max_age_seconds=self.max_record_age_seconds)
        fatal = [item for item in blockers if item.get("reason") != "no_screened_candidate_in_the_record"]
        if fatal:
            return self._refuse(fatal[0]["reason"], fatal[0]["detail"])
        rows = list(record.paper_set)
        if not rows:
            return self._refuse(
                "no_screened_candidate_in_the_record",
                "No candidate passed the screen on the data that was evaluated, so "
                "there is nothing to paper trade yet.",
            )
        wanted = {str(key) for key in (keys or ()) if str(key)}
        if wanted:
            chosen = [row for row in rows if paper_row_key(row) in wanted]
            if not chosen:
                return self._refuse(
                    "none_of_the_chosen_candidates",
                    "None of the candidates you ticked is in the screened set on "
                    "record. Re-run the evaluation, or tick one of the rows it "
                    "lists; nothing was widened back to all of them.",
                )
            rows = chosen
        terminal = self._terminal()
        if terminal is None:
            return self._refuse(
                "no_terminal",
                "Connect the terminal first: the paper test prices fills from its "
                "quotes, and there is no other price source.",
            )
        try:
            names = list(terminal.symbol_names())
        except Exception:
            names = []
        watches, refused = watches_from_rows(rows, symbol_names=names, limit=limit)
        if not watches:
            return self._refuse(
                "no_usable_watch",
                "None of the screened rows can be followed right now: the terminal does "
                "not offer those symbols, or they are not declared for that timeframe.",
                rows=list(refused),
            )
        with self._lock:
            self._paper = CatalogPaperTest(
                quotes=terminal,
                watches=watches,
                root=self.data_root / "paper",
                lot=self.lot,
                clock=self.clock,
            )
            self._paper_watches = watches
        return self._ok(
            f"Built a paper test over {len(watches)} screened row(s): "
            + ", ".join(sorted({watch.symbol for watch in watches}))
            + ". Press Start to begin.",
            path="/quick-setup",
        )

    def _paper_start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self._paper
        if current is not None and current.running:
            return self._refuse(
                "paper_test_already_running",
                "A live-market paper test is already running. Stop it before starting "
                "another one; the running test is untouched.",
            )
        # Rebuild on every start, so a newer evaluation is picked up and the watch
        # list is re-checked against what the terminal offers right now.  Open paper
        # positions come back from the state file.
        built = self.build_paper_test(keys=_as_list(payload.get("paper")))
        if not built.get("ok"):
            return built
        with self._lock:
            paper = self._paper
        if paper is None:  # pragma: no cover - build_paper_test sets it
            return self._refuse("paper_test_not_built", "The paper test could not be built.")
        result = paper.start()
        if not result.get("started"):
            blocker = (result.get("blockers") or [{}])[0]
            return self._refuse(
                str(blocker.get("reason") or "paper_start_refused"),
                str(blocker.get("detail") or "The paper test did not start."),
            )
        return self._ok(
            "The live-market paper test is running: real terminal quotes, locally "
            "simulated fills, no order sent.",
            path="/quick-setup",
        )

    def _paper_stop(self) -> dict[str, Any]:
        with self._lock:
            paper = self._paper
        if paper is None:
            return self._refuse("paper_test_not_built", "No paper test is built.")
        result = paper.stop()
        return self._ok(
            f"Paper test stopped ({result.get('status')}). The ledger is on disk.",
            path="/quick-setup",
        )

    def _paper_flat(self) -> dict[str, Any]:
        with self._lock:
            paper = self._paper
        if paper is None:
            return self._refuse("paper_test_not_built", "No paper test is built.")
        result = paper.close_all()
        return self._ok(
            f"Simulated closing {result.get('closed', 0)} open paper position(s).",
            path="/quick-setup",
        )

    # -- plumbing ----------------------------------------------------------

    def _terminal(self) -> Any:
        try:
            return self.terminal_provider()
        except Exception:
            return None

    def _ok(self, message: str, *, path: str = "/quick-setup", rows: Any = None) -> dict[str, Any]:
        outcome: dict[str, Any] = {"ok": True, "message": message, "path": path}
        if rows is not None:
            outcome["rows"] = list(rows)
        self._last_outcome = outcome
        return outcome

    def _refuse(self, reason: str, message: str, *, rows: Any = None) -> dict[str, Any]:
        outcome: dict[str, Any] = {"ok": False, "reason": reason, "message": message, "path": "/quick-setup"}
        if rows is not None:
            outcome["rows"] = list(rows)
        self._last_outcome = outcome
        return outcome

    def stop(self) -> None:
        """Stop whatever this panel started; used when the process shuts down."""

        self._run.cancelled.set()
        with self._lock:
            paper = self._paper
        if paper is not None:
            try:
                paper.stop(reason="workstation_stopped")
            except Exception:
                pass


def _classify_all(facts: Sequence[SymbolFacts]) -> tuple[Any, ...]:
    from axiom_forex.config.fx_symbols import classify

    return tuple(classify(item) for item in facts)


#: Row sources that mean "these bars came from the terminal's own rate history".
TERMINAL_SOURCES = ("MT5_COPY_RATES", "MT5_TERMINAL", "TERMINAL")


def _origin_for(series: Any) -> str:
    """Label where a series' bars came from, without inflating the label."""

    source = str(getattr(series, "source", "") or "").upper()
    if any(marker in source for marker in TERMINAL_SOURCES):
        return "MT5_TERMINAL_READ_ONLY"
    return source or "UNKNOWN_ORIGIN"


def _point(facts: SymbolFacts | None) -> float | None:
    if facts is None or facts.point is None:
        return None
    return float(facts.point)


def paper_row_key(row: Mapping[str, Any]) -> str:
    """The stable name of one screened row: symbol, timeframe, candidate.

    It is what the start page's tick boxes carry, and it is derived from the
    record rather than from the page, so a row that is no longer on record simply
    does not match.
    """

    return "|".join(
        str(row.get(field) or "") for field in ("symbol", "timeframe", "candidate_id")
    )


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


__all__ = [
    "ACTIONS",
    "ACTION_EVALUATE",
    "ACTION_PAPER_FLAT",
    "ACTION_PAPER_START",
    "ACTION_PAPER_STOP",
    "CatalogPanel",
    "DEFAULT_BARS",
    "DEFAULT_EVAL_TIMEFRAME",
    "EVAL_TIMEFRAMES",
    "MAX_EVAL_SYMBOLS",
    "MAX_RECORD_AGE_SECONDS",
    "paper_row_key",
]
