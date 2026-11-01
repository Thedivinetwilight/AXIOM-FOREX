"""The short-setup and one-year-research panel behind ``/quick-setup``.

The panel is the browser layer's whole contact with the research path:

* it reads the connected terminal **read-only** (account facts and symbol specs),
  through the same surface the observation session already exposes;
* it asks for the five things a terminal cannot answer and records them, with
  provenance, in ``quick_setup.local.json``;
* it fetches one year of broker bars through the terminal's ``rates()`` and stores
  them, labelled, under ``data/history/research/``;
* it runs the sweep on a worker thread so the page stays responsive, reports
  progress, and can be cancelled;
* it reports *which* symbols and timeframes have usable history, and everything
  that did not run, with a reason.

It cannot connect, cannot send an order, cannot change the mode, and cannot start
the paper runner: starting paper trading stays on the existing runner action, and
the page only says what that action still needs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from axiom_forex.config.quick_setup import (
    DEFAULT_WINDOW_DAYS,
    FILENAME as QUICK_SETUP_FILENAME,
    CostAssumptions,
    QuickChoices,
    QuickSetup,
    QuickSetupError,
    compose,
    load_quick_setup,
    summarise,
)
from axiom_forex.config.terminal_facts import (
    UNKNOWN,
    TerminalFacts,
    observe_terminal,
)

from .engine import CostModel, SweepReport, cost_model_for, record_attempts, run_sweep
from .history import (
    TIMEFRAMES as HISTORY_TIMEFRAMES,
    HistoryStore,
    fetch_series,
)

#: The research window this build offers.
WINDOW_DAYS = DEFAULT_WINDOW_DAYS

#: Default timeframes to fetch when the operator does not choose: a year of the
#: slower bars is small enough to fetch and quick enough to evaluate.
DEFAULT_TIMEFRAMES: tuple[str, ...] = ("1h", "4h", "1d")

#: How many table rows the page renders before it stops and says so.
MAX_ROWS_SHOWN = 60


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc()).isoformat()


class ResearchRefused(Exception):
    """The panel cannot do what was asked.  Carries a stable reason code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = str(reason)
        self.detail = str(detail)


@dataclass
class ResearchPanel:
    """Quick setup, stored history, and the sweep, with no trading surface."""

    root: Path
    terminal_provider: Callable[[], Any] | None = None
    setup_path: Path | None = None
    ledger_path: Path | None = None
    store: HistoryStore | None = None
    #: Where stored history lives.  Defaults to ``data/history/research`` under
    #: the repository root, which this project already ignores.
    store_root: Path | None = None
    runner: Any = None
    #: Frames to read from the terminal per symbol: a year of 1m bars is large,
    #: so the panel refuses rather than truncating (see ``history.MAX_ROWS``).
    symbol_limit: int = 60
    budget_seconds: float = 540.0

    _report: SweepReport | None = field(default=None, init=False, repr=False)
    _result: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _progress: dict[str, Any] = field(
        default_factory=lambda: {"running": False, "fraction": 0.0, "label": ""},
        init=False,
        repr=False,
    )
    _cancel: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.setup_path = Path(self.setup_path or self.root / QUICK_SETUP_FILENAME)
        self.store = self.store or HistoryStore(
            Path(self.store_root or (self.root / "data" / "history" / "research"))
        )

    # -- reading ----------------------------------------------------------

    def _terminal(self) -> Any:
        if self.terminal_provider is None:
            raise ResearchRefused(
                "no_terminal",
                "This process has no read-only terminal surface, so no symbol or "
                "spec can be read.",
            )
        try:
            return self.terminal_provider()
        except Exception as exc:
            raise ResearchRefused(
                "terminal_not_connected",
                "No terminal session is open, so there is nothing to read.",
            ) from exc

    def facts(self) -> TerminalFacts:
        """One read of the terminal, or a labelled reason why there is none."""

        try:
            terminal = self._terminal()
        except ResearchRefused as refusal:
            return TerminalFacts(error=refusal.reason, notes=(refusal.detail,))
        return observe_terminal(terminal, symbol_limit=self.symbol_limit)

    def load_setup(self) -> QuickSetup | None:
        try:
            return load_quick_setup(self.setup_path)
        except QuickSetupError:
            return None
        except Exception:
            return None

    def inventory(self) -> list[dict[str, Any]]:
        return self.store.inventory() if self.store is not None else []

    # -- the page ---------------------------------------------------------

    def view(self) -> dict[str, Any]:
        facts = self.facts()
        saved = self.load_setup()
        choices = saved.choices if saved is not None else QuickChoices()
        readiness = (
            saved.readiness()
            if saved is not None
            else {"research": {"ready": False, "blockers": [], "note": ""},
                  "live": {"ready": False, "blockers": [], "note": ""}}
        )
        observed_rows = [
            {"label": label, "value": value, "state": state, "detail": detail}
            for label, value, state, detail in summarise(
                saved if saved is not None else compose(facts, choices)
            )
        ]
        inventory = self.inventory()
        report = self._report
        view: dict[str, Any] = {
            "available": True,
            "facts": facts.audit_view(),
            "symbols": [
                {
                    "broker_symbol": item.broker_symbol,
                    "pair": item.pair,
                    "tradeable": item.tradable,
                    "volume_min": str(item.volume_min) if item.volume_min else "",
                    "volume_step": str(item.volume_step) if item.volume_step else "",
                    "stops_level": item.stops_level,
                    "swap_state": item.swap_state,
                    "digits": item.digits,
                    "missing": list(item.missing()),
                }
                for item in facts.symbols
            ],
            "observed_rows": observed_rows,
            "choices": choices.audit_view(),
            "saved": (
                {
                    "path": str(self.setup_path),
                    "fingerprint": saved.fingerprint(),
                    "created_at": saved.created_at,
                    "readiness": readiness,
                    "costs": saved.costs.audit_view(),
                }
                if saved is not None
                else None
            ),
            "readiness": readiness if saved is not None else None,
            "timeframes": list(HISTORY_TIMEFRAMES),
            "default_timeframes": [
                timeframe
                for timeframe in DEFAULT_TIMEFRAMES
                if timeframe in HISTORY_TIMEFRAMES
            ],
            "history": inventory,
            "history_usable": [
                item for item in inventory if item.get("usable_for_research")
            ],
            "window_days": WINDOW_DAYS,
            "progress": dict(self._progress),
            "running": bool(self._progress.get("running")),
            "report": (
                {
                    "summary": report.summary(),
                    "table": report.table()[:MAX_ROWS_SHOWN],
                    "row_count": len(report.table()),
                    "coverage": [
                        dict(item.coverage.audit_view()) for item in report.series
                    ],
                    "not_run": [dict(item) for item in report.not_run],
                    "failures": [dict(item) for item in report.failures],
                    "notes": list(report.notes),
                    "claim": report.claim,
                    "walk_forward": [
                        {
                            "symbol": item.symbol,
                            "timeframe": item.timeframe,
                            "variant": item.selected_variant_id,
                            **item.walk_forward.audit_view(),
                        }
                        for item in report.series
                        if item.walk_forward is not None
                    ],
                    "sensitivity": [
                        {
                            "symbol": item.symbol,
                            "timeframe": item.timeframe,
                            "variant": item.selected_variant_id,
                            "runs": list(item.sensitivity),
                        }
                        for item in report.series
                        if item.sensitivity
                    ],
                }
                if report is not None
                else None
            ),
            "paper": self.paper_plan(),
        }
        if self._result:
            view["result"] = dict(self._result)
            self._result = {}
        return view

    # -- actions ----------------------------------------------------------

    def act(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if action == "quick-setup-save":
                return self._save(payload)
            if action == "research-fetch":
                return self._fetch(payload)
            if action == "research-run":
                return self._run()
            if action == "research-cancel":
                return self._cancel_run()
            raise ResearchRefused("unknown_action", action)
        except ResearchRefused as refusal:
            return {
                "ok": False,
                "reason": refusal.reason,
                "message": refusal.detail or refusal.reason,
                "path": "/quick-setup",
            }
        except Exception as exc:  # never leak a traceback into the page
            return {
                "ok": False,
                "reason": type(exc).__name__,
                "message": (
                    "The research panel could not finish that step "
                    f"({type(exc).__name__}). Nothing was traded, and no mode changed."
                ),
                "path": "/quick-setup",
            }

    def _save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        facts = self.facts()
        choices = QuickChoices.from_form(payload)
        setup = compose(facts, choices, self._costs_from(payload))
        problems = choices.problems()
        if problems:
            raise ResearchRefused("choices_incomplete", "; ".join(problems))
        saved = setup.save(self.setup_path)
        self._result = {
            "ok": True,
            "message": (
                "Setup recorded with its provenance. Research readiness: "
                + (
                    "ready."
                    if setup.readiness()["research"]["ready"]
                    else "not yet -- see the blockers."
                )
            ),
            "path": "/quick-setup",
            "fingerprint": saved.get("fingerprint", ""),
        }
        return self._result

    @staticmethod
    def _costs_from(payload: Mapping[str, Any]) -> CostAssumptions:
        def number(name: str, fallback: str) -> Any:
            raw = str(payload.get(name) or "").strip()
            if not raw:
                return fallback
            try:
                from decimal import Decimal

                return Decimal(raw)
            except Exception:
                return fallback

        from decimal import Decimal

        return CostAssumptions(
            commission_per_lot=number("commission_per_lot", "0"),
            slippage_pips=number("slippage_pips", "0.2"),
            sensitivity_multipliers=(Decimal("1"), Decimal("2")),
        )

    def _fetch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        setup = self.load_setup()
        if setup is None:
            raise ResearchRefused(
                "quick_setup_missing",
                "Save the short setup first: the research run needs to know which "
                "symbols and which size you chose.",
            )
        selected = setup.selected()
        if not selected:
            raise ResearchRefused(
                "no_observed_symbols",
                "None of the chosen symbols was reported by the terminal, so there is "
                "nothing to fetch.",
            )
        requested = [str(item) for item in payload.get("timeframes") or [] if str(item)]
        timeframes = tuple(
            item for item in (requested or DEFAULT_TIMEFRAMES) if item in HISTORY_TIMEFRAMES
        )
        if not timeframes:
            raise ResearchRefused(
                "no_timeframe_selected",
                "Choose at least one timeframe to fetch.",
            )
        try:
            terminal = self._terminal()
        except ResearchRefused as refusal:
            raise refusal

        rows: list[dict[str, Any]] = []
        stored = 0
        for symbol in selected:
            for timeframe in timeframes:
                outcome = fetch_series(
                    terminal,
                    symbol=symbol.broker_symbol,
                    timeframe=timeframe,
                    pair=symbol.pair,
                    point=float(symbol.point or 0),
                    window_days=int(setup.choices.window_days or WINDOW_DAYS),
                )
                row = outcome.summary_row()
                if outcome.ok and self.store is not None:
                    try:
                        self.store.save(outcome)
                        stored += 1
                        row["stored"] = True
                    except Exception as exc:
                        row["stored"] = False
                        row["reason"] = f"could not be stored ({type(exc).__name__})"
                rows.append(row)
        usable = [row for row in rows if row.get("usable")]
        message = (
            f"Read {len(rows)} symbol/timeframe request(s): {len(usable)} usable, "
            f"{stored} stored locally. Anything not usable is listed with its reason."
        )
        self._result = {
            "ok": bool(usable),
            "message": message,
            "path": "/quick-setup",
            "rows": rows,
        }
        return self._result

    def _run(self) -> dict[str, Any]:
        if self._progress.get("running"):
            raise ResearchRefused(
                "already_running", "A research run is already in progress."
            )
        setup = self.load_setup()
        if setup is None:
            raise ResearchRefused(
                "quick_setup_missing", "Save the short setup before running research."
            )
        if not setup.readiness()["research"]["ready"]:
            blockers = setup.readiness()["research"]["blockers"]
            raise ResearchRefused(
                "research_not_ready",
                "The research run still needs: " + "; ".join(blockers),
            )
        loaded = self.store.load_all() if self.store is not None else {}
        wanted = {item.broker_symbol for item in setup.selected()}
        series_by_key = {
            key: stored.series
            for key, stored in loaded.items()
            if key[0] in wanted and stored.usable_for_research
        }
        if not series_by_key:
            # Say exactly which stored series were rejected and why, instead of
            # quietly running on nothing.
            rejected = [
                item
                for item in self.inventory()
                if item.get("symbol") in wanted
            ]
            if rejected:
                reasons = "; ".join(
                    f"{item['symbol']} {item['timeframe']}: "
                    f"{item.get('reason') or 'not usable'}"
                    for item in rejected
                )
                raise ResearchRefused(
                    "stored_history_not_usable",
                    "The stored history for the chosen symbols is not usable yet "
                    f"-- {reasons}. Fetch again once the terminal has more bars.",
                )
            raise ResearchRefused(
                "no_stored_history",
                "No stored terminal history matches the chosen symbols. Fetch history "
                "first -- this panel never substitutes fixtures for market data.",
            )
        costs: dict[str, CostModel] = {}
        for symbol in setup.selected():
            if symbol.broker_symbol not in {key[0] for key in series_by_key}:
                continue
            model = cost_model_for(
                symbol,
                account_currency=setup.facts.currency.value or "",
                commission_per_lot=float(setup.costs.commission_per_lot),
                slippage_pips=float(setup.costs.slippage_pips),
            )
            # A symbol whose point, contract size, slippage or commission is not
            # a real number cannot be priced; skipping it keeps ``nan`` out of
            # every figure instead of reporting a meaningless one.
            if model.unusable:
                continue
            costs[symbol.broker_symbol] = model
        if not costs:
            raise ResearchRefused(
                "specs_unknown",
                "A symbol's point size or contract size is unknown, so no cost model "
                "could be built for it.",
            )
        self._cancel.clear()
        self._progress = {
            "running": True,
            "fraction": 0.0,
            "label": "starting",
            "started_at": _iso(),
        }
        thread = threading.Thread(target=self._work, args=(series_by_key, costs, setup), daemon=True)
        self._thread = thread
        thread.start()
        return {
            "ok": True,
            "message": (
                "Research started: one pass over the stored history, inside a "
                f"{self.budget_seconds:.0f}-second budget. The page shows progress."
            ),
            "path": "/quick-setup",
        }

    def _work(
        self,
        series_by_key: Mapping[tuple[str, str], Any],
        costs: Mapping[str, CostModel],
        setup: QuickSetup,
    ) -> None:
        def progress(fraction: float, label: str) -> None:
            self._progress = {
                **self._progress,
                "fraction": float(fraction),
                "label": str(label),
            }

        try:
            report = run_sweep(
                series_by_key,
                costs=costs,
                lot_size=float(setup.choices.lot_size or 0),
                window_days=int(setup.choices.window_days or WINDOW_DAYS),
                budget_seconds=self.budget_seconds,
                cancel=self._cancel.is_set,
                progress=progress,
            )
        except Exception as exc:
            self._progress = {
                "running": False,
                "fraction": 1.0,
                "label": f"failed ({type(exc).__name__})",
            }
            self._result = {
                "ok": False,
                "message": (
                    "The research run stopped on an internal error "
                    f"({type(exc).__name__}). Nothing was traded."
                ),
                "path": "/quick-setup",
            }
            return
        self._report = report
        if self.ledger_path is not None:
            try:
                from axiom_forex.strategies.library.ledger import StrategyLedger

                ledger = StrategyLedger(self.ledger_path)
                record_attempts(report, ledger, note="one_year_terminal_history")
            except Exception:
                # A ledger that cannot be written must not hide the report; the
                # report itself says the attempt is unrecorded.
                pass
        self._progress = {
            "running": False,
            "fraction": 1.0,
            "label": report.status,
            "finished_at": _iso(),
        }
        summary = report.summary()
        measured = int(summary["variants_measured"])
        if not measured:
            message = (
                "The research run finished but measured nothing: no stored series "
                "passed the coverage audit. The reasons are listed per symbol and "
                "timeframe above."
            )
        else:
            message = (
                f"Research {report.status.lower().replace('_', ' ')}: {measured} of "
                f"{summary['variants_total']} variants measured on "
                f"{summary['series_with_usable_history']} usable series in "
                f"{summary['elapsed_seconds']:.0f}s. "
                + (
                    f"{summary['not_run']} item(s) did not run and are listed."
                    if summary["not_run"]
                    else "Everything that was in scope ran."
                )
                + " These are historical estimates under the stated cost assumptions, "
                "not a forecast."
            )
        self._result = {
            "ok": report.status == "COMPLETE" and bool(measured),
            "message": message,
            "path": "/quick-setup",
        }

    def _cancel_run(self) -> dict[str, Any]:
        if not self._progress.get("running"):
            raise ResearchRefused("not_running", "No research run is in progress.")
        self._cancel.set()
        self._result = {
            "ok": True,
            "message": (
                "Cancelling: the run stops at the next unit and reports what it "
                "measured, with the rest listed as not run."
            ),
            "path": "/quick-setup",
        }
        return self._result

    # -- paper ------------------------------------------------------------

    def paper_plan(self) -> dict[str, Any]:
        """What paper trading still needs, in the operator's own terms.

        This never starts anything.  The start itself stays on the runner action,
        which owns the gates, the feed, and the simulated fills.
        """

        blockers: list[dict[str, str]] = []
        setup = self.load_setup()
        if setup is None:
            blockers.append(
                {
                    "reason": "quick_setup_missing",
                    "message": "Record the short setup first; paper trading needs the "
                    "symbols and the size you chose.",
                    "hint": "Save the setup on this page.",
                }
            )
        else:
            readiness = setup.readiness()["research"]
            if not readiness["ready"]:
                blockers.append(
                    {
                        "reason": "research_not_ready",
                        "message": "The setup is not research-ready yet.",
                        "hint": "; ".join(readiness["blockers"][:3]),
                    }
                )
        selection = self.selected_strategy()
        if selection is None:
            blockers.append(
                {
                    "reason": "no_strategy_selected",
                    "message": "No strategy has been selected by the operator, so paper "
                    "trading has nothing to run.",
                    "hint": "Evaluate and select a variant on the strategy library page.",
                }
            )
        runner = self._runner_state()
        if not runner.get("available"):
            blockers.append(
                {
                    "reason": "runner_not_configured",
                    "message": "This workstation was started without a paper runner.",
                    "hint": "Start it with the paper-runner options; nothing else is needed.",
                }
            )
        elif runner.get("running"):
            blockers = [
                item for item in blockers if item["reason"] not in {"runner_not_configured"}
            ]
        return {
            "ready": not blockers,
            "blockers": blockers,
            "selection": selection,
            "runner": runner,
            "note": (
                "Paper fills are simulated locally from the terminal's current quotes. "
                "Starting the runner never changes the execution mode, never arms live, "
                "and never sends an order."
            ),
        }

    def _runner_state(self) -> dict[str, Any]:
        if self.runner is None:
            return {"available": False, "running": False}
        try:
            view = dict(self.runner.view() or {})
        except Exception:
            return {"available": False, "running": False, "error": "runner_state_unreadable"}
        return {
            "available": True,
            "running": bool(view.get("running")),
            "state": str(view.get("state") or ""),
        }

    def selected_strategy(self) -> dict[str, Any] | None:
        """The operator's selection, read from the library ledger (read-only)."""

        if self.ledger_path is None:
            return None
        try:
            from axiom_forex.strategies.library.ledger import StrategyLedger

            ledger = StrategyLedger(self.ledger_path)
            selections = ledger.selections()
        except Exception:
            return None
        if not selections:
            return None
        first = dict(selections[0])
        return {
            "variant_id": str(first.get("variant_id") or ""),
            "symbol": str(first.get("symbol") or ""),
            "timeframe": str(first.get("timeframe") or ""),
            "selected_at": str(first.get("selected_at") or ""),
        }


__all__ = [
    "DEFAULT_TIMEFRAMES",
    "MAX_ROWS_SHOWN",
    "ResearchPanel",
    "ResearchRefused",
    "UNKNOWN",
    "WINDOW_DAYS",
]
