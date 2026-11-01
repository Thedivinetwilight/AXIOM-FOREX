"""Which screened candidates become the running plan, and why every other one did not.

Everything here is a **pure function of evidence that was already recorded**.  It
reads no terminal, runs no simulation, and writes nothing.  That matters because
the decision it makes -- *this candidate will now run* -- is the one place where an
automatic workflow could quietly become an automated search.

The rules, fixed before any data is read
----------------------------------------
``screen``            a row must have passed the catalog's own screen, which reads
                      the **train and validation** windows only (see
                      :mod:`axiom_forex.strategies.catalog.evaluate`).  A row that
                      failed it is rejected with the screen's own wording.
``folds``             a row needs at least ``min_folds_positive`` walk-forward
                      folds that are profitable **and lie wholly inside train plus
                      validation**.  The evaluator's folds span the entire series,
                      test window included, so using them wholesale would leak the
                      locked test set into selection.  Folds that end after the
                      validation boundary are not counted, and a row whose boundary
                      cannot be established is rejected rather than counted.
``drawdown``          a validation drawdown above ``max_drawdown`` is rejected.
``concentration``     a validation result carried by one trade is rejected.
``costs``            a cost model with a defective term is rejected.  A cost model
                      that is usable but *incomplete* is not: the assumption is
                      carried on the plan and shown, never converted into a claim.
``data origin``       a plan built from fixture or simulated bars selects nothing.
                      Selection is an evidence-based promotion, so its evidence has
                      to be the broker's own history.

Then, and only then, ranking: ``score`` (the weaker of train and validation net
return, penalised by validation drawdown), descending, with a **deterministic
tie-break** on candidate id, symbol and timeframe.  Caps are applied in ranked
order -- per symbol, per timeframe, per structure, and in total -- so the same
inputs always produce the same plan, byte for byte.

What this module cannot do: claim a candidate is profitable, retune a parameter,
change a mode, or trade.  A selected row is a short-listed hypothesis with
out-of-sample support on the data that was evaluated.  The UI says so on every
screen that shows one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

#: A row's decision.  ``ELIGIBLE_NOT_CHOSEN`` is not a rejection: the row passed
#: every rule and lost on ranking or a cap, which is a different fact and is shown
#: as one.
SELECTED = "SELECTED"
REJECTED = "REJECTED"
ELIGIBLE = "ELIGIBLE_NOT_CHOSEN"

#: Why a row was not selected.  Stable codes, so the UI and the tests agree.
REASON_NOT_TESTED = "row_never_ran"
REASON_SCREEN_FAILED = "failed_the_screen"
REASON_SHORT_SAMPLE = "too_few_trades_to_judge"
REASON_NOT_SCREENED = "not_screened"
REASON_NO_SCORE = "no_score_on_record"
REASON_DEFECTIVE_COST = "cost_model_defective"
REASON_STRUCTURE_UNKNOWN = "candidate_not_in_this_catalog"
REASON_BOUNDARY_UNKNOWN = "validation_boundary_unknown"
REASON_FOLDS_OUT_OF_SAMPLE = "too_few_in_sample_folds"
REASON_RANKED_BELOW = "ranked_below_the_selected_rows"
REASON_CAP_SYMBOL = "symbol_capacity_reached"
REASON_CAP_TIMEFRAME = "timeframe_capacity_reached"
REASON_CAP_STRUCTURE = "structure_already_running"
REASON_CAP_TOTAL = "selection_capacity_reached"

#: Plan-level blockers (nothing can be selected at all).
BLOCKER_NO_ROWS = "no_rows_to_select_from"
BLOCKER_NOT_EVALUATED = "evaluation_did_not_complete"
BLOCKER_DATA_ORIGIN = "selection_requires_terminal_history"
BLOCKER_POLICY_INVALID = "selection_policy_invalid"
BLOCKER_NO_SELECTION = "no_candidate_met_the_selection_rules"

#: The one label every automatic decision carries.
SELECTION_NOTE = (
    "Selected on train and validation evidence only, with the parameters frozen "
    "before the data was read. The test window was not used to choose anything. A "
    "selected row is a short-listed hypothesis, not a profitable strategy, and "
    "nothing here is a forecast or a profit claim."
)

#: Data origins whose bars are the broker's own history.  Anything else -- a
#: fixture, a sample, a simulated preview -- can be studied but cannot promote.
PROMOTING_ORIGINS = frozenset({"MT5_TERMINAL_READ_ONLY", "MT5_TERMINAL"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _when(text: Any) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _finite(value: Any) -> float | None:
    """A real number, or ``None``.  ``nan`` and ``inf`` are not evidence."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectionPolicy:
    """The rules, declared once and fingerprinted into every plan."""

    #: Walk-forward folds that must be positive *and* inside train+validation.
    min_folds_positive: int = 1
    #: Validation drawdown this row may show.  Matches the evaluator's screen.
    max_drawdown: float = 0.25
    #: Share of the validation result one trade may carry.
    max_concentration: float = 0.6
    #: Caps, applied in ranked order.  Deterministic, so the plan is reproducible.
    max_per_symbol: int = 1
    max_per_timeframe: int = 2
    max_per_structure: int = 1
    max_selected: int = 6
    #: A plan built on anything but the terminal's own history selects nothing.
    require_promoting_origin: bool = True

    def problems(self) -> tuple[str, ...]:
        found: list[str] = []
        if self.min_folds_positive < 0:
            found.append("min_folds_positive must not be negative")
        for name in ("max_drawdown", "max_concentration"):
            value = _finite(getattr(self, name))
            if value is None or value < 0:
                found.append(f"{name} must be a finite, non-negative number")
        for name in ("max_per_symbol", "max_per_timeframe", "max_per_structure", "max_selected"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                found.append(f"{name} must be a positive whole number")
        if self.max_selected < 1:
            found.append("max_selected must be at least 1")
        return tuple(found)

    def fingerprint(self) -> str:
        payload = {
            "min_folds_positive": self.min_folds_positive,
            "max_drawdown": round(float(self.max_drawdown), 6),
            "max_concentration": round(float(self.max_concentration), 6),
            "max_per_symbol": self.max_per_symbol,
            "max_per_timeframe": self.max_per_timeframe,
            "max_per_structure": self.max_per_structure,
            "max_selected": self.max_selected,
            "require_promoting_origin": self.require_promoting_origin,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def audit_view(self) -> dict[str, Any]:
        return {
            "min_folds_positive": self.min_folds_positive,
            "max_drawdown": self.max_drawdown,
            "max_concentration": self.max_concentration,
            "max_per_symbol": self.max_per_symbol,
            "max_per_timeframe": self.max_per_timeframe,
            "max_per_structure": self.max_per_structure,
            "max_selected": self.max_selected,
            "require_promoting_origin": self.require_promoting_origin,
            "fingerprint": self.fingerprint(),
            "fold_scope": "folds_must_end_at_or_before_the_validation_boundary",
            "test_window_used": False,
        }


# ---------------------------------------------------------------------------
# one decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """One row's verdict, with the evidence it rested on and the wording for it."""

    key: str
    candidate_id: str
    symbol: str
    timeframe: str
    family: str
    structure: str
    status: str
    reasons: tuple[str, ...] = ()
    message: str = ""
    rank: int | None = None
    score: float | None = None
    folds_in_sample: int = 0
    folds_total: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def selected(self) -> bool:
        return self.status == SELECTED

    def audit_view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "family": self.family,
            "structure": self.structure,
            "status": self.status,
            "reasons": list(self.reasons),
            "message": self.message,
            "rank": self.rank,
            "score": None if self.score is None else round(self.score, 6),
            "folds_in_sample": self.folds_in_sample,
            "folds_total": self.folds_total,
            "evidence": dict(self.evidence),
        }

    def compact(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "family": self.family,
            "structure": self.structure,
            "status": self.status,
            "reasons": list(self.reasons),
            "message": self.message,
            "rank": self.rank,
            "score": None if self.score is None else round(self.score, 6),
            "folds_in_sample": self.folds_in_sample,
            "folds_total": self.folds_total,
        }


@dataclass(frozen=True)
class SelectionPlan:
    """Every decision from one selection pass, plus what the pass rested on."""

    decisions: tuple[Decision, ...] = ()
    policy: SelectionPolicy = field(default_factory=SelectionPolicy)
    data_origin: str = ""
    evaluated_at: str = ""
    generated_at: str = ""
    blockers: tuple[dict[str, str], ...] = ()
    counts: Mapping[str, Any] = field(default_factory=dict)
    costs: Mapping[str, Any] = field(default_factory=dict)
    note: str = SELECTION_NOTE

    # -- views -----------------------------------------------------------

    def selected(self) -> tuple[Decision, ...]:
        return tuple(item for item in self.decisions if item.status == SELECTED)

    def rejected(self) -> tuple[Decision, ...]:
        return tuple(item for item in self.decisions if item.status == REJECTED)

    def eligible(self) -> tuple[Decision, ...]:
        """Rows that passed every rule and lost on ranking or a cap."""

        return tuple(item for item in self.decisions if item.status == ELIGIBLE)

    @property
    def available(self) -> bool:
        return bool(self.selected())

    def rows(self) -> tuple[dict[str, Any], ...]:
        """The selected rows, in ranked order, in the stored-record shape.

        The catalog's own paper path already knows how to turn one of these rows
        into a watch, so the plan stays the single source of what runs.
        """

        out: list[dict[str, Any]] = []
        for item in self.selected():
            out.append(
                {
                    "candidate_id": item.candidate_id,
                    "symbol": item.symbol,
                    "timeframe": item.timeframe,
                    "score": item.score,
                    "folds_positive": item.folds_in_sample,
                    "selection": item.audit_view(),
                }
            )
        return tuple(out)

    def summary(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "selected": len(self.selected()),
            "rejected": len(self.rejected()),
            "eligible_not_chosen": len(self.eligible()),
            "blockers": [dict(item) for item in self.blockers],
            "counts": dict(self.counts),
            "policy": self.policy.audit_view(),
            "data_origin": self.data_origin,
            "evaluated_at": self.evaluated_at,
            "generated_at": self.generated_at,
            "note": self.note,
        }

    def audit_view(self, limit: int = 200) -> dict[str, Any]:
        payload = self.summary()
        kept = self.decisions[:limit]
        payload["decisions"] = [item.audit_view() for item in kept]
        payload["decisions_omitted"] = max(0, len(self.decisions) - len(kept))
        payload["costs"] = dict(self.costs)
        return payload

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe, and small enough to keep beside the evaluation record."""

        payload = self.summary()
        payload["decisions"] = [item.compact() for item in self.decisions]
        payload["costs"] = {key: dict(value) for key, value in self.costs.items()}
        return payload


# ---------------------------------------------------------------------------
# reading a row
# ---------------------------------------------------------------------------


def _as_row(item: Any) -> dict[str, Any]:
    """One row as a plain mapping, whether it is a Row or a stored dict."""

    if isinstance(item, Mapping):
        return dict(item)
    compact = getattr(item, "compact", None)
    if callable(compact):
        return dict(compact())
    return {
        field_name: getattr(item, field_name, None)
        for field_name in (
            "candidate_id",
            "family",
            "structure",
            "symbol",
            "timeframe",
            "status",
            "reasons",
            "screen",
            "screen_reasons",
            "score",
            "concentration",
            "costs_incomplete",
            "folds_positive",
            "walk_forward",
            "windows",
        )
    }


def in_sample_folds(
    row: Mapping[str, Any], boundary: datetime | None
) -> tuple[int, int]:
    """Positive folds that end at or before the validation boundary.

    Returns ``(positive_in_sample, total_in_sample)``.  A fold whose window cannot
    be read is not counted: an unreadable fold is not evidence of stability.
    """

    folds = row.get("walk_forward") or row.get("folds") or ()
    if not isinstance(folds, Sequence) or isinstance(folds, (str, bytes)):
        return 0, 0
    positive = 0
    total = 0
    for fold in folds:
        if not isinstance(fold, Mapping):
            continue
        end = _when(fold.get("end"))
        if end is None:
            continue
        if boundary is not None and end > boundary:
            continue
        total += 1
        net = _finite(fold.get("net_return"))
        trades = int(fold.get("trades") or 0)
        if net is not None and net > 0 and trades > 0:
            positive += 1
    return positive, total


def validation_boundaries(slots: Iterable[Any]) -> dict[tuple[str, str], datetime]:
    """The last instant of each slot's validation window, from the slot itself.

    This is what makes the fold rule honest: the evaluator's folds cover the whole
    series, so only folds ending at or before this instant are out-of-sample-within
    -sample evidence rather than a look at the locked test window.
    """

    from .evaluate import split_bounds

    out: dict[tuple[str, str], datetime] = {}
    for slot in slots or ():
        series = getattr(slot, "series", None)
        stamps = list(getattr(series, "timestamps", ()) or ())
        if not stamps:
            continue
        bounds = split_bounds(len(series))
        start, end = bounds["validation"]
        if end - start <= 0:
            continue
        last = end - 1
        if 0 <= last < len(stamps):
            moment = stamps[last]
            if isinstance(moment, datetime):
                out[(str(slot.symbol), str(slot.timeframe))] = (
                    moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
                )
    return out


# ---------------------------------------------------------------------------
# the selector
# ---------------------------------------------------------------------------


def _evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """The train/validation evidence, and nothing from the test window.

    ``windows["test"]`` is deliberately not read anywhere in this module.  It is
    copied out here so the record can say the run measured it, but no rule below
    looks at it.
    """

    windows = row.get("windows") or {}
    out: dict[str, Any] = {}
    if isinstance(windows, Mapping):
        for name in ("train", "validation"):
            block = windows.get(name)
            if isinstance(block, Mapping):
                out[name] = {
                    "trades": int(block.get("trades") or 0),
                    "net_return": _finite(block.get("net_return")),
                    "max_drawdown": _finite(block.get("max_drawdown")),
                    "cost_pips": _finite(block.get("cost_pips")),
                }
    out["costs_incomplete"] = bool(row.get("costs_incomplete"))
    out["concentration"] = _finite(row.get("concentration"))
    out["bars"] = int(row.get("bars") or 0)
    return out


def _screen_reasons(row: Mapping[str, Any]) -> tuple[str, ...]:
    reasons = row.get("screen_reasons") or ()
    return tuple(str(item) for item in reasons if str(item).strip())


def _key_of(row: Mapping[str, Any]) -> str:
    return (
        f"{row.get('candidate_id', '')}@{row.get('symbol', '')}"
        f"|{row.get('timeframe', '')}"
    )


def select(
    rows: Iterable[Any],
    *,
    policy: SelectionPolicy | None = None,
    slots: Iterable[Any] = (),
    data_origin: str = "",
    evaluated_at: str = "",
    evaluated_status: str = "",
    now: datetime | None = None,
) -> SelectionPlan:
    """Choose the rows that run, and record why every other row does not.

    Deterministic: the same rows, policy and boundaries always produce the same
    plan.  Nothing is retried, relaxed or widened to fill the cap -- an empty
    selection is a result, and it is reported with its reason.
    """

    chosen_policy = policy or SelectionPolicy()
    moment = now or _utc_now()
    items = [_as_row(row) for row in rows]
    problems = chosen_policy.problems()

    blockers: list[dict[str, str]] = []
    if problems:
        blockers.append(
            {
                "reason": BLOCKER_POLICY_INVALID,
                "detail": "The selection rules are not usable: " + "; ".join(problems),
            }
        )
    if evaluated_status and evaluated_status not in ("COMPLETE", "TIMEOUT_PARTIAL"):
        blockers.append(
            {
                "reason": BLOCKER_NOT_EVALUATED,
                "detail": (
                    f"The evaluation finished as {evaluated_status}, so its rows are not "
                    "a basis for selecting anything."
                ),
            }
        )
    origin = str(data_origin or "").strip()
    if chosen_policy.require_promoting_origin and origin not in PROMOTING_ORIGINS:
        blockers.append(
            {
                "reason": BLOCKER_DATA_ORIGIN,
                "detail": (
                    "The evaluation did not rest on the terminal's own history "
                    f"(origin: {origin or 'unknown'}). A sample or fixture run can be "
                    "studied, but it cannot promote a strategy to trading."
                ),
            }
        )
    if not items:
        blockers.append(
            {
                "reason": BLOCKER_NO_ROWS,
                "detail": "The evaluation produced no rows, so there is nothing to select.",
            }
        )

    boundaries = validation_boundaries(slots)
    bound_known = bool(boundaries)

    decisions: list[Decision] = []
    ranked: list[Decision] = []

    for row in items:
        candidate_id = str(row.get("candidate_id") or "")
        symbol = str(row.get("symbol") or "")
        timeframe = str(row.get("timeframe") or "")
        key = _key_of(row)
        family = str(row.get("family") or "")
        structure = str(row.get("structure") or "")
        evidence = _evidence(row)
        status = str(row.get("status") or "")
        screen = str(row.get("screen") or "")
        score = _finite(row.get("score"))
        folds_total_all = len(row.get("walk_forward") or ())
        boundary = boundaries.get((symbol, timeframe))
        positive, total = in_sample_folds(row, boundary)
        if boundary is None:
            # Without the validation boundary the fold evidence cannot be trusted,
            # so it is not counted at all.
            positive, total = 0, 0

        reasons: list[str] = []
        message = ""

        if blockers:
            # A plan-level blocker stops the pass; each row still says what it is.
            reasons.append(blockers[0]["reason"])
            message = blockers[0]["detail"]
        elif status != "TESTED":
            reasons.append(REASON_NOT_TESTED)
            detail = ", ".join(str(item) for item in (row.get("reasons") or ())) or status
            message = f"The row never produced a result ({detail}), so it cannot run."
        elif screen == "FAILED_SCREEN":
            reasons.append(REASON_SCREEN_FAILED)
            message = "Failed the screen on train and validation: " + "; ".join(
                _screen_reasons(row)
            )
        elif screen == "TOO_FEW_TRADES":
            reasons.append(REASON_SHORT_SAMPLE)
            message = "Too few trades in train or validation to judge: " + "; ".join(
                _screen_reasons(row)
            )
        elif screen != "PASSED_SCREEN":
            reasons.append(REASON_NOT_SCREENED)
            message = f"The row was not screened ({screen or 'no screen state'})."
        elif score is None:
            reasons.append(REASON_NO_SCORE)
            message = "The screen recorded no score, so the row cannot be ranked."
        elif not structure:
            reasons.append(REASON_STRUCTURE_UNKNOWN)
            message = "The row does not name a structure that this build's catalog has."
        elif boundary is None and chosen_policy.min_folds_positive > 0:
            reasons.append(REASON_BOUNDARY_UNKNOWN)
            message = (
                "The validation boundary for this symbol and timeframe is unknown, so "
                "the walk-forward folds cannot be shown to be out of the locked test "
                "window. Re-evaluate with the bar series in hand."
            )
        elif positive < chosen_policy.min_folds_positive:
            reasons.append(REASON_FOLDS_OUT_OF_SAMPLE)
            message = (
                f"{positive} profitable walk-forward fold(s) inside train and "
                f"validation; {chosen_policy.min_folds_positive} required. Folds that "
                f"run into the test window are not counted "
                f"({folds_total_all} fold(s) measured overall)."
            )

        decision = Decision(
            key=key,
            candidate_id=candidate_id,
            symbol=symbol,
            timeframe=timeframe,
            family=family,
            structure=structure,
            status=REJECTED if reasons else ELIGIBLE,
            reasons=tuple(reasons),
            message=message,
            score=score,
            folds_in_sample=positive,
            folds_total=folds_total_all,
            evidence=evidence,
        )
        decisions.append(decision)
        if not reasons:
            ranked.append(decision)

    # Deterministic ranking: score, then the ids.  No randomness, no clock.
    ranked.sort(key=lambda item: (-(item.score or 0.0), item.candidate_id, item.symbol, item.timeframe))

    per_symbol: dict[str, int] = {}
    per_timeframe: dict[str, int] = {}
    per_structure: dict[str, int] = {}
    keep: list[Decision] = []
    chosen_keys: set[str] = set()
    for position, item in enumerate(ranked, start=1):
        if len(keep) >= chosen_policy.max_selected:
            break
        if per_symbol.get(item.symbol, 0) >= chosen_policy.max_per_symbol:
            continue
        if per_timeframe.get(item.timeframe, 0) >= chosen_policy.max_per_timeframe:
            continue
        if per_structure.get(item.structure, 0) >= chosen_policy.max_per_structure:
            continue
        keep.append(item)
        chosen_keys.add(item.key)
        per_symbol[item.symbol] = per_symbol.get(item.symbol, 0) + 1
        per_timeframe[item.timeframe] = per_timeframe.get(item.timeframe, 0) + 1
        per_structure[item.structure] = per_structure.get(item.structure, 0) + 1

    # Rebuild the decision list with ranks and the cap wording.
    final: list[Decision] = []
    rank_of = {item.key: index for index, item in enumerate(keep, start=1)}
    for item in decisions:
        if item.key in chosen_keys:
            final.append(
                Decision(
                    key=item.key,
                    candidate_id=item.candidate_id,
                    symbol=item.symbol,
                    timeframe=item.timeframe,
                    family=item.family,
                    structure=item.structure,
                    status=SELECTED,
                    message=(
                        "Selected at rank "
                        f"{rank_of[item.key]} on train and validation evidence "
                        "(score from the weaker of the two windows, less drawdown). "
                        "The test window took no part in this decision."
                    ),
                    rank=rank_of[item.key],
                    score=item.score,
                    folds_in_sample=item.folds_in_sample,
                    folds_total=item.folds_total,
                    evidence=item.evidence,
                )
            )
            continue
        if item.status != ELIGIBLE:
            final.append(item)
            continue
        # Eligible but not chosen: say which cap stopped it.
        reason, message = _cap_reason(item, chosen_policy, per_symbol, per_timeframe, per_structure, len(keep))
        final.append(
            Decision(
                key=item.key,
                candidate_id=item.candidate_id,
                symbol=item.symbol,
                timeframe=item.timeframe,
                family=item.family,
                structure=item.structure,
                status=ELIGIBLE,
                reasons=(reason,),
                message=message,
                rank=None,
                score=item.score,
                folds_in_sample=item.folds_in_sample,
                folds_total=item.folds_total,
                evidence=item.evidence,
            )
        )

    # The plan reads best in decision order: the rows that run first, in rank
    # order, then everyone else in the order the evaluation produced them.  The
    # ordering is a function of the rows alone, so two identical evaluations always
    # serialise identically.
    selected_in_order = sorted(
        (item for item in final if item.status == SELECTED), key=lambda item: item.rank or 0
    )
    held_back = sorted(
        (item for item in final if item.status == ELIGIBLE),
        key=lambda item: (-(item.score or 0.0), item.key),
    )
    rejected = sorted((item for item in final if item.status == REJECTED), key=lambda item: item.key)
    final = selected_in_order + held_back + rejected

    if not blockers and not keep:
        blockers.append(
            {
                "reason": BLOCKER_NO_SELECTION,
                "detail": (
                    "No row passed the selection rules on this evaluation. That is a "
                    "result, not a failure: nothing is selected, and no rule was "
                    "relaxed to fill the plan."
                ),
            }
        )

    counts = {
        "rows": len(final),
        "selected": len(keep),
        "rejected": sum(1 for item in final if item.status == REJECTED),
        "eligible_not_chosen": sum(1 for item in final if item.status == ELIGIBLE),
        "folds_out_of_sample_only": True,
        "validation_boundary_known": bound_known,
    }
    return SelectionPlan(
        decisions=tuple(final),
        policy=chosen_policy,
        data_origin=origin,
        evaluated_at=str(evaluated_at or ""),
        generated_at=moment.isoformat(),
        blockers=tuple(blockers),
        counts=counts,
        costs={},
        note=SELECTION_NOTE,
    )


def _cap_reason(
    item: Decision,
    policy: SelectionPolicy,
    per_symbol: Mapping[str, int],
    per_timeframe: Mapping[str, int],
    per_structure: Mapping[str, int],
    selected_count: int,
) -> tuple[str, str]:
    """The one cap that actually stopped this row, in ranking order."""

    if selected_count >= policy.max_selected:
        return (
            REASON_CAP_TOTAL,
            f"The plan holds {policy.max_selected} rows, its declared maximum; this row "
            "ranked behind them.",
        )
    if per_structure.get(item.structure, 0) >= policy.max_per_structure:
        return (
            REASON_CAP_STRUCTURE,
            "Another row with the same structure is already in the plan, and the rules "
            "keep one per structure so the plan is not six variants of one idea.",
        )
    if per_symbol.get(item.symbol, 0) >= policy.max_per_symbol:
        return (
            REASON_CAP_SYMBOL,
            f"{item.symbol} already has {policy.max_per_symbol} row(s) in the plan.",
        )
    if per_timeframe.get(item.timeframe, 0) >= policy.max_per_timeframe:
        return (
            REASON_CAP_TIMEFRAME,
            f"{item.timeframe} already has {policy.max_per_timeframe} row(s) in the plan.",
        )
    return (
        REASON_RANKED_BELOW,
        "It passed every rule but ranked below the rows that were selected.",
    )


def plan_from_report(
    report: Any,
    *,
    policy: SelectionPolicy | None = None,
    slots: Iterable[Any] = (),
    data_origin: str = "",
    now: datetime | None = None,
) -> SelectionPlan:
    """Select from an :class:`EvaluationReport`, or from its stored payload."""

    if hasattr(report, "rows") and hasattr(report, "status"):
        return select(
            report.rows,
            policy=policy,
            slots=slots,
            data_origin=data_origin,
            evaluated_at=getattr(report, "finished_at", "") or "",
            evaluated_status=str(getattr(report, "status", "") or ""),
            now=now,
        )
    payload = dict(report or {})
    return select(
        payload.get("rows") or (),
        policy=policy,
        slots=slots,
        data_origin=data_origin or str(payload.get("data_origin") or ""),
        evaluated_at=str(payload.get("finished_at") or payload.get("written_at") or ""),
        evaluated_status=str(payload.get("status") or ""),
        now=now,
    )


__all__ = [
    "BLOCKER_DATA_ORIGIN",
    "BLOCKER_NOT_EVALUATED",
    "BLOCKER_NO_ROWS",
    "BLOCKER_NO_SELECTION",
    "BLOCKER_POLICY_INVALID",
    "Decision",
    "ELIGIBLE",
    "PROMOTING_ORIGINS",
    "REASON_BOUNDARY_UNKNOWN",
    "REASON_CAP_SYMBOL",
    "REASON_CAP_STRUCTURE",
    "REASON_CAP_TIMEFRAME",
    "REASON_CAP_TOTAL",
    "REASON_DEFECTIVE_COST",
    "REASON_FOLDS_OUT_OF_SAMPLE",
    "REASON_NOT_SCREENED",
    "REASON_NOT_TESTED",
    "REASON_NO_SCORE",
    "REASON_RANKED_BELOW",
    "REASON_SCREEN_FAILED",
    "REASON_SHORT_SAMPLE",
    "REASON_STRUCTURE_UNKNOWN",
    "REJECTED",
    "SELECTED",
    "SELECTION_NOTE",
    "SelectionPlan",
    "SelectionPolicy",
    "in_sample_folds",
    "plan_from_report",
    "select",
    "validation_boundaries",
]
