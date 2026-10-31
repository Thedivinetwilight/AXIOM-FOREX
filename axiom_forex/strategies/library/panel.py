"""The state behind ``/strategies``: the catalogue, the attempts, the choice.

The page is a research index, not an order screen.  It can:

* show every template, its source, its rules, its parameters, the timeframes it
  declares, its assumptions and its licence;
* show how many variants of each template exist and how many were actually
  attempted -- the honest count, not a library size;
* screen recorded evaluations with the predeclared criteria and the
  multiple-testing control, and record every attempt in the ledger;
* show which symbols and timeframes can be monitored, and why a timeframe is
  ignored;
* record the operator's explicit selection of an eligible variant.

It cannot select for the operator, choose a symbol, size a position, arm
anything, or change an execution mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from axiom_forex.paper_runner.strategy import STATUS_SELECTED, Candidate

from .cards import (
    CARDS,
    DEFAULT_CAPABILITIES,
    FAMILIES,
    Capabilities,
    StrategyCard,
    card as find_card,
    family_label,
    search,
)
from .ledger import ELIGIBLE, SelectionRefused, StrategyLedger
from .monitor import (
    DEFAULT_CRITERIA,
    AttemptInput,
    ScreenCriteria,
    coverage,
    explain,
    score_search,
)
from .variants import GRID_VERSION, RuleStrategy, Variant, count_variants, variants_for

LEDGER_FILENAME = "strategy_attempts.sqlite3"

SELECTION_NOTE = (
    "This is the operator's own choice. It does not size a position, set a stop, "
    "arm anything, or change the execution mode: those are separate steps on the "
    "Live execution page, and the existing risk gate still applies."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _result(ok: bool, message: str, *, reason: str = "") -> dict[str, Any]:
    return {"ok": ok, "message": message, "reason": reason, "path": "/strategies"}


@dataclass
class StrategyLibraryPanel:
    root: Path
    #: ``(symbol, timeframe, count) -> bars``; ``None`` means no bar source, so
    #: coverage cannot be measured and the page says so.
    bars_provider: Callable[[str, str, int], Sequence[Any]] | None = None
    #: ``(card_id, variant_id, symbol, timeframe) -> evaluation payload`` from the
    #: paper lane's own evaluator. ``None`` means this deployment cannot screen
    #: recorded evidence.
    evidence_provider: Callable[[str, str, str, str], Mapping[str, Any] | None] | None = None
    #: ``(symbol) -> platform`` with a ``new_engine`` factory, used only by the
    #: explicit evaluate action. ``None`` means this deployment cannot run one.
    platform_builder: Callable[[str], Any] | None = None
    capabilities: Capabilities = DEFAULT_CAPABILITIES
    criteria: ScreenCriteria = DEFAULT_CRITERIA
    clock: Callable[[], datetime] = _utc_now
    logger: Callable[[str], None] | None = None

    _ledger: StrategyLedger | None = None
    _notice: dict[str, Any] = field(default_factory=dict)

    @property
    def ledger_path(self) -> Path:
        return Path(self.root) / LEDGER_FILENAME

    def ledger(self) -> StrategyLedger:
        if self._ledger is None:
            self._ledger = StrategyLedger(self.ledger_path, clock=self.clock)
        return self._ledger

    # -- searching ------------------------------------------------------

    def catalogue(self, *, text: str = "", family: str = "") -> list[dict[str, Any]]:
        found = search(text=text, family=family, capabilities=self.capabilities)
        ledger = self.ledger()
        best = ledger.best_attempt_per_variant()
        selections = {item["variant_id"]: item for item in ledger.selections()}
        rows: list[dict[str, Any]] = []
        for item in found:
            availability = item.availability(self.capabilities)
            variants = variants_for(item)
            attempted = [variant for variant in variants if variant.variant_id in best]
            eligible = [
                variant
                for variant in attempted
                if best[variant.variant_id].status == ELIGIBLE
            ]
            selected = [
                variant for variant in variants if variant.variant_id in selections
            ]
            rows.append(
                {
                    **item.audit_view(),
                    "family_label": family_label(item.family),
                    "availability": availability,
                    "variants": len(variants),
                    "attempted": len(attempted),
                    "eligible": len(eligible),
                    "selected": selected[0].variant_id if selected else "",
                    "status": _card_status(item, availability, attempted, eligible, selected),
                    "variants_detail": self.variant_rows(item.card_id),
                }
            )
        return rows

    def variant_rows(self, card_id: str) -> list[dict[str, Any]]:
        item = find_card(card_id)
        if item is None:
            return []
        ledger = self.ledger()
        best = ledger.best_attempt_per_variant()
        selections = {entry["variant_id"]: entry for entry in ledger.selections()}
        rows = []
        for variant in variants_for(item):
            attempt = best.get(variant.variant_id)
            rows.append(
                {
                    **variant.audit_view(),
                    "attempt": attempt.audit_view() if attempt else {},
                    "status": attempt.status if attempt else "NOT_ATTEMPTED",
                    "selected": variant.variant_id in selections,
                }
            )
        return rows

    # -- actions --------------------------------------------------------

    def screen(self, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Screen every variant of one template (or all) against recorded evidence."""

        payload = _payload(form)
        card_id = str(payload.get("card_id", "") or "").strip()
        symbol = str(payload.get("symbol", "") or "").strip()
        timeframe = str(payload.get("timeframe", "") or "").strip()
        if self.evidence_provider is None:
            return _result(
                False,
                "No evaluation source is wired in, so there is nothing to screen. "
                "Evaluate a candidate on Data & evaluation first.",
                reason="no_evidence_source",
            )
        cards = [find_card(card_id)] if card_id else list(CARDS)
        cards = [item for item in cards if item is not None]
        inputs: list[AttemptInput] = []
        missing: list[str] = []
        for item in cards:
            if not item.availability(self.capabilities)["evaluable"]:
                continue
            timeframes = [timeframe] if timeframe else list(item.timeframes)
            for variant in variants_for(item):
                for tf in timeframes:
                    try:
                        evidence = self.evidence_provider(item.card_id, variant.variant_id, symbol, tf)
                    except Exception:
                        evidence = None
                    if not evidence:
                        continue
                    inputs.append(
                        AttemptInput(
                            variant_id=variant.variant_id,
                            card_id=item.card_id,
                            evidence=evidence,
                            symbol=symbol or str(evidence.get("symbol", "")),
                            timeframe=tf,
                        )
                    )
            missing.append(item.card_id)
        if not inputs:
            return _result(
                False,
                "No recorded evaluation matched these variants, so nothing was "
                "screened and nothing was recorded as a result.",
                reason="no_evidence",
            )
        report = score_search(inputs, criteria=self.criteria)
        recorded = 0
        for entry in report["results"]:
            item = find_card(entry["card_id"])
            variant = next(
                (v for v in variants_for(item) if v.variant_id == entry["variant_id"]), None
            ) if item else None
            evidence = next(
                (
                    member.evidence
                    for member in inputs
                    if member.variant_id == entry["variant_id"]
                ),
                {},
            )
            self.ledger().record(
                variant_id=entry["variant_id"],
                card_id=entry["card_id"],
                grid_version=GRID_VERSION,
                symbol=entry["symbol"] or symbol,
                timeframe=entry["timeframe"],
                dataset_id=str(evidence.get("dataset_id", "")),
                dataset_fingerprint=str(evidence.get("dataset_fingerprint", evidence.get("dataset_id", ""))),
                status=entry["verdict"]["status"],
                parameters=(variant.parameters if variant else {}),
                metrics=evidence,
                reason=entry["verdict"]["reason"],
                checks=entry["verdict"]["checks"],
                p_value=str(evidence.get("bootstrap_p_value", "")),
                q_value=str(entry["q_value"]),
                reality_check=str(entry["reality_check_p_value"]),
            )
            recorded += 1
        accepted = len(report["accepted"])
        self._notice = {
            "ok": True,
            "message": (
                f"Screened {recorded} attempt(s): {accepted} eligible, "
                f"{recorded - accepted} rejected or insufficient. Every attempt is "
                "recorded, including the failures."
            ),
        }
        return _result(True, self._notice["message"])

    def evaluate(self, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Evaluate one variant on one declared dataset, then record the attempt.

        Running the backtest is an explicit operator action.  Both the data class
        and the dataset id must be typed by the operator: AXIOM never guesses which
        history this is, because the answer decides whether a result may ever
        promote a strategy.
        """

        payload = _payload(form)
        variant_id = str(payload.get("variant_id", "") or "").strip()
        symbol = str(payload.get("symbol", "") or "").strip()
        timeframe = str(payload.get("timeframe", "") or "").strip()
        dataset_id = str(payload.get("dataset_id", "") or "").strip()
        data_class = str(payload.get("data_class", "") or "").strip().upper()

        if self.platform_builder is None or self.bars_provider is None:
            return _result(
                False,
                "Evaluating here needs both an engine and bars from this feed; this "
                "deployment has neither wired in. Use Data & evaluation, or the CLI.",
                reason="no_evaluation_source",
            )
        if not (variant_id and symbol and timeframe and dataset_id and data_class):
            return _result(
                False,
                "Variant, symbol, timeframe, dataset id and data class are all "
                "required. The data class decides whether a result may promote a "
                "strategy, so AXIOM will not fill it in.",
                reason="missing_fields",
            )
        variant = _variant_by_id(variant_id)
        if variant is None:
            return _result(False, f"{variant_id} is not a library variant.", reason="unknown_variant")
        try:
            bars = list(self.bars_provider(symbol, timeframe, 600))
        except Exception as exc:
            return _result(
                False,
                f"No bars were available for {symbol} {timeframe} "
                f"({getattr(exc, 'reason', type(exc).__name__)}).",
                reason="no_bars",
            )
        from .evaluation import EvaluationRefused, evaluate_variant

        try:
            platform = self.platform_builder(symbol)
        except Exception as exc:
            return _result(
                False,
                f"The evaluation platform could not be built ({type(exc).__name__}).",
                reason="platform_unavailable",
            )
        try:
            result = evaluate_variant(
                variant,
                bars=bars,
                symbol=symbol,
                timeframe=timeframe,
                data_class=data_class,
                dataset_id=dataset_id,
                engine_factory=platform.new_engine,
                criteria=self.criteria,
                clock=self.clock,
            )
        except EvaluationRefused as exc:
            return _result(False, f"Not evaluated ({exc.reason}): {exc.detail}", reason=exc.reason)
        except Exception as exc:
            return _result(
                False,
                f"The evaluation stopped ({type(exc).__name__}). Nothing was recorded.",
                reason="evaluation_failed",
            )
        try:
            platform.close()
        except Exception:
            pass

        attempt = self.ledger().record(
            variant_id=result.variant.variant_id,
            card_id=result.variant.card_id,
            grid_version=GRID_VERSION,
            symbol=result.symbol,
            timeframe=result.timeframe,
            dataset_id=result.dataset_id,
            dataset_fingerprint=result.dataset_fingerprint,
            status=str(result.verdict.get("status", "")),
            parameters=result.variant.parameters,
            metrics=dict(result.evidence),
            reason=str(result.verdict.get("reason", "")),
            checks=result.verdict.get("checks") or (),
            p_value=str(result.evidence.get("bootstrap_p_value", "")),
            q_value=result.q_value,
            reality_check=result.reality_check,
        )
        return _result(
            True,
            f"Recorded attempt {attempt.attempt_id}: {result.verdict.get('status')}. "
            + result.explanation,
        )

    def select(self, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = _payload(form)
        variant_id = str(payload.get("variant_id", "") or "").strip()
        confirmed = str(payload.get("confirmed", "") or "").strip().lower() in {"yes", "on", "1", "true"}
        if not variant_id:
            return _result(False, "Choose a variant first.", reason="variant_required")
        if not confirmed:
            return _result(
                False,
                "Tick the confirmation first: selecting records your own choice, and "
                "it is not made for you.",
                reason="confirmation_required",
            )
        attempts = self.ledger().attempts(variant_id=variant_id, limit=50)
        eligible = [item for item in attempts if item.status == ELIGIBLE]
        if not eligible:
            return _result(
                False,
                "That variant has no eligible attempt recorded, so it cannot be "
                "selected. A rejected or untested variant is never promoted.",
                reason="not_eligible",
            )
        try:
            selection = self.ledger().select(eligible[0].attempt_id, note="operator selection")
        except SelectionRefused as exc:
            return _result(False, f"Selection refused ({exc.reason}).", reason=exc.reason)
        return _result(
            True,
            f"Selected {variant_id} on {selection['symbol']} {selection['timeframe']}, "
            "from the recorded attempt. " + SELECTION_NOTE,
        )

    def deselect(self, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
        variant_id = str(_payload(form).get("variant_id", "") or "").strip()
        if not variant_id:
            return _result(False, "Choose a variant first.", reason="variant_required")
        removed = self.ledger().deselect(variant_id)
        return _result(
            True,
            f"Deselected {variant_id}." if removed else f"{variant_id} was not selected.",
        )

    def monitor(self) -> dict[str, Any]:
        if self.bars_provider is None:
            return _result(
                False,
                "Monitoring needs bars from this feed, and none are wired in.",
                reason="no_bar_source",
            )
        rows = self._coverage_rows()
        monitorable = [row for row in rows if row["monitorable"]]
        return _result(
            True,
            f"{len(monitorable)} of {len(rows)} symbol/timeframe combination(s) can be "
            "monitored; the rest are listed with the reason they are ignored.",
        )

    def _coverage_rows(self) -> list[dict[str, Any]]:
        symbols = sorted({str(item) for item in getattr(self, "_symbols", ()) or ()})
        if not symbols:
            return []
        timeframes = sorted({tf for item in CARDS for tf in item.timeframes})
        counts: dict[tuple[str, str], int] = {}
        if self.bars_provider is not None:
            for symbol in symbols:
                for timeframe in timeframes:
                    try:
                        counts[(symbol, timeframe)] = len(
                            list(self.bars_provider(symbol, timeframe, 512))
                        )
                    except Exception:
                        counts[(symbol, timeframe)] = 0
        return coverage(
            symbols=symbols,
            timeframes=timeframes,
            bar_counts=counts,
            capabilities=self.capabilities,
        )

    # -- the live path's view of a selection -----------------------------

    def selected_candidates(self) -> list[Candidate]:
        """Operator-selected variants, shaped like the paper lane's candidates.

        The evidence travels with them, so the live page can label an UNVALIDATED
        strategy as such and :func:`status_of` can refuse one that is not eligible.
        """

        found: list[Candidate] = []
        for selection in self.ledger().selections():
            variant = _variant_by_id(selection["variant_id"])
            if variant is None:
                continue
            attempt = self.ledger().get(selection["attempt_id"])
            if attempt is None or attempt.status != ELIGIBLE:
                continue
            evidence = dict(attempt.metrics or {})
            evidence.setdefault("evaluated", True)
            evidence.setdefault("data_class", str(evidence.get("data_class", "")))
            evidence.setdefault("passed", True)
            evidence.setdefault("failed_criteria", [])
            evidence.setdefault("criteria_version", self.criteria.version)
            evidence["selected_by_operator"] = True
            found.append(
                Candidate(
                    candidate_id=f"library::{selection['variant_id']}",
                    strategy_factory=lambda _variant=variant, **_: RuleStrategy(_variant),
                    parameters=dict(variant.parameters),
                    rationale=(
                        f"{variant.card.name} from the strategy library; the operator "
                        "selected this variant explicitly on "
                        f"{selection['symbol']} {selection['timeframe']}."
                    ),
                    assumptions=variant.card.assumptions,
                    limitations=variant.card.limitations,
                    status=STATUS_SELECTED,
                    evidence=evidence,
                )
            )
        return found

    # -- view -----------------------------------------------------------

    def view(self, *, query: Mapping[str, Any] | None = None, symbols: Iterable[str] = ()) -> dict[str, Any]:
        params = query or {}
        text = str(_first(params.get("q")) or "").strip()
        family = str(_first(params.get("family")) or "").strip()
        self._symbols = [str(item) for item in symbols]
        ledger = self.ledger()
        counts = ledger.counts()
        catalogue = self.catalogue(text=text, family=family)
        rows = self._coverage_rows()
        return {
            "available": True,
            "query": {"text": text, "family": family},
            "families": [{"key": key, "label": label} for key, label in FAMILIES],
            "catalogue": catalogue,
            "counts": counts,
            "library": count_variants(),
            "criteria": self.criteria.audit_view(),
            "grid_version": GRID_VERSION,
            "selections": ledger.selections(),
            "coverage": rows,
            "has_bar_source": self.bars_provider is not None,
            "has_evidence_source": self.evidence_provider is not None,
            "has_evaluation": self.platform_builder is not None and self.bars_provider is not None,
            "multiplicity_note": (
                "Every attempted variant is recorded. The screen corrects for the "
                "number of attempts with Benjamini–Hochberg and asks White's Reality "
                "Check whether the best variant beats luck; neither turns a historical "
                "result into a forecast."
            ),
            "non_claims": [
                "A larger library is not a better one: the count above is variants of "
                "documented templates, not independently researched strategies.",
                "Sources are reasons to test a shape, never proof that it works.",
                "Nothing here can select a strategy for you, size a position, place an "
                "order, arm live execution, or change an execution mode.",
                "A selected strategy still has to pass the live plan's review, the "
                "confirmation phrase, the risk gate, and the protective-stop rule.",
            ],
        }

    # -- dispatch --------------------------------------------------------

    def act(self, action: str, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if action == "strategy-screen":
            return self.screen(form)
        if action == "strategy-evaluate":
            return self.evaluate(form)
        if action == "strategy-select":
            return self.select(form)
        if action == "strategy-deselect":
            return self.deselect(form)
        if action == "strategy-monitor":
            return self.monitor()
        return _result(False, f"Unknown strategy action {action!r}.", reason="unknown_action")


def _card_status(
    item: StrategyCard,
    availability: Mapping[str, Any],
    attempted: Sequence[Variant],
    eligible: Sequence[Variant],
    selected: Sequence[Variant],
) -> str:
    if selected:
        return "SELECTED_BY_OPERATOR"
    if not availability.get("evaluable"):
        return "UNAVAILABLE"
    if eligible:
        return "ELIGIBLE_VARIANT"
    if attempted:
        return "ATTEMPTED"
    return "TEMPLATE"


def _variant_by_id(variant_id: str) -> Variant | None:
    for item in CARDS:
        for variant in variants_for(item):
            if variant.variant_id == variant_id:
                return variant
    return None


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value


def _payload(form: Mapping[str, Any] | None) -> dict[str, Any]:
    if not form:
        return {}
    return {str(key): _first(value) for key, value in form.items() if str(key) != "nonce"}


def default_panel(root: str | Path, **kwargs: Any) -> StrategyLibraryPanel:
    return StrategyLibraryPanel(root=Path(root), **kwargs)


__all__ = [
    "LEDGER_FILENAME",
    "SELECTION_NOTE",
    "StrategyLibraryPanel",
    "default_panel",
]
