"""The operator's live plan: what, how much, and how far it may go wrong.

A live plan is the answer to five questions the *operator* must answer before a
single real order can be sent:

* which strategy,
* which symbol,
* what order size,
* how many positions may be open at once,
* and how much loss in the account currency stops everything.

Nothing here chooses any of them.  There is no default candidate, no default
pair, no default lot size, and no default limit.  A missing answer is a named
problem, not a substituted value.

The plan is also the *ceiling*: it may only tighten what the provider setup
already allows.  A plan that asks for a larger position count or a larger daily
loss than ``provider_setup.local.json`` declares is refused, because the setup is
the document the risk gate enforces and a per-run file must never widen it.

The file itself is ``live_plan.local.json`` in the project root, matched by the
repository's ``*.local.json`` ignore rule.  Writing it arms nothing: the file is
a declaration of intent, and the only thing that can authorise an order is the
in-process arming decision described in :mod:`axiom_forex.paper_runner.live`.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA = "axiom-live-plan-v1"
PLAN_FILENAME = "live_plan.local.json"
SOURCE = "OPERATOR_LIVE_PLAN"

#: How the evidence behind a candidate is described in the plan and on the page.
EVIDENCE_MEASURED = "MEASURED"
EVIDENCE_UNVALIDATED = "UNVALIDATED"

#: Keys the plan may never carry, whatever the caller passes.
CREDENTIAL_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "secret",
        "api_key",
        "investor_password",
        "market_access_token",
    }
)


class PlanError(Exception):
    """The plan document cannot be used.  Carries a stable reason code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = str(reason)
        self.detail = str(detail)

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class PlanProblem:
    """One reason the plan the operator typed cannot be used."""

    path: str
    message: str

    def audit_view(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _instant(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evidence_state(evidence: Mapping[str, Any] | None) -> str:
    """``MEASURED`` only when the candidate has evaluation evidence that passed."""

    if not evidence:
        return EVIDENCE_UNVALIDATED
    if not evidence.get("evaluated"):
        return EVIDENCE_UNVALIDATED
    if evidence.get("passed") is not True:
        return EVIDENCE_UNVALIDATED
    return EVIDENCE_MEASURED


def evidence_note(state: str, evidence: Mapping[str, Any] | None) -> str:
    """One honest sentence about what is and is not known about this strategy."""

    if state == EVIDENCE_UNVALIDATED:
        return (
            "This strategy has no measured result behind it. Selecting it for live "
            "trading is a decision the operator is making without evidence."
        )
    payload = dict(evidence or {})
    parts = []
    if payload.get("dataset_id"):
        parts.append(f"dataset {payload['dataset_id']}")
    if payload.get("data_class"):
        parts.append(f"data class {payload['data_class']}")
    if payload.get("criteria_version"):
        parts.append(f"criteria {payload['criteria_version']}")
    suffix = f" ({', '.join(parts)})" if parts else ""
    return (
        "This strategy met the predeclared criteria on historical data"
        + suffix
        + ". That is a historical measurement, not a forecast, and not a promise "
        "of profit."
    )


def candidate_choices(candidates: Iterable[Any]) -> list[dict[str, Any]]:
    """Every candidate the operator may pick, with its evidence labelled."""

    choices: list[dict[str, Any]] = []
    for candidate in candidates:
        evidence = dict(getattr(candidate, "evidence", {}) or {})
        state = evidence_state(evidence)
        choices.append(
            {
                "candidate_id": str(getattr(candidate, "candidate_id", "")),
                "parameters": dict(getattr(candidate, "parameters", {}) or {}),
                "rationale": str(getattr(candidate, "rationale", "")),
                "evidence_state": state,
                "evidence_note": evidence_note(state, evidence),
                "evidence": evidence,
            }
        )
    return choices


def build_plan(
    values: Mapping[str, Any],
    *,
    setup: Any,
    candidates: Sequence[Any],
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, tuple[PlanProblem, ...]]:
    """Validate what the operator typed.  Returns the document, or every problem.

    No value is defaulted.  A blank field is a problem with that field's path,
    and the document is only returned when there is not a single problem.
    """

    problems: list[PlanProblem] = []

    def text(name: str) -> str:
        return str(values.get(name, "") or "").strip()

    candidate_id = text("candidate_id")
    candidate = next(
        (item for item in candidates if str(getattr(item, "candidate_id", "")) == candidate_id),
        None,
    )
    if not candidate_id:
        problems.append(PlanProblem("plan.candidate_id", "choose the strategy that may trade live"))
    elif candidate is None:
        problems.append(
            PlanProblem("plan.candidate_id", f"{candidate_id} is not one of the known candidates")
        )

    pairs = tuple(setup.pairs()) if setup is not None else ()
    pair = text("pair")
    if not pair:
        problems.append(
            PlanProblem(
                "plan.pair",
                "choose the symbol; "
                + (
                    "your provider setup declares " + ", ".join(pairs)
                    if pairs
                    else "no provider setup is loaded, so there is nothing to choose from"
                ),
            )
        )
    elif setup is None:
        problems.append(
            PlanProblem("plan.pair", "no provider setup is loaded, so no symbol can be resolved")
        )
    elif setup.terms_for(pair) is None:
        problems.append(
            PlanProblem(
                "plan.pair",
                f"{pair} is not in your provider setup (declared: {', '.join(pairs) or 'none'})",
            )
        )

    quantity = _decimal(values.get("quantity"))
    if quantity is None:
        problems.append(PlanProblem("plan.quantity", "enter the order size in lots"))
    elif quantity <= 0:
        problems.append(PlanProblem("plan.quantity", "the order size must be greater than zero"))

    max_open = text("max_open_positions")
    parsed_max_open: int | None = None
    if not max_open:
        problems.append(
            PlanProblem("plan.max_open_positions", "enter how many positions may be open at once")
        )
    else:
        try:
            parsed_max_open = int(Decimal(max_open))
        except (InvalidOperation, ValueError):
            problems.append(
                PlanProblem("plan.max_open_positions", "use a whole number of positions")
            )
        else:
            if parsed_max_open < 1:
                problems.append(
                    PlanProblem("plan.max_open_positions", "at least one position must be allowed")
                )

    max_loss = _decimal(values.get("max_daily_loss_account"))
    if max_loss is None:
        problems.append(
            PlanProblem(
                "plan.max_daily_loss_account",
                "enter the daily loss limit in the account currency",
            )
        )
    elif max_loss <= 0:
        problems.append(
            PlanProblem("plan.max_daily_loss_account", "the loss limit must be greater than zero")
        )

    # The plan may tighten the setup, never widen it.
    risk = getattr(setup, "risk", None) if setup is not None else None
    if risk is not None:
        setup_positions = int(getattr(risk, "max_open_positions", 0) or 0)
        if parsed_max_open is not None and setup_positions and parsed_max_open > setup_positions:
            problems.append(
                PlanProblem(
                    "plan.max_open_positions",
                    f"your provider setup allows at most {setup_positions} open position(s); "
                    "a live plan may tighten that, not raise it",
                )
            )
        setup_loss = _decimal(getattr(risk, "max_daily_loss_account", None))
        if max_loss is not None and setup_loss is not None and max_loss > setup_loss:
            problems.append(
                PlanProblem(
                    "plan.max_daily_loss_account",
                    f"your provider setup's daily loss limit is {setup_loss}; "
                    "a live plan may tighten that, not raise it",
                )
            )

    if problems or candidate is None or setup is None:
        return None, tuple(problems)

    terms = setup.terms_for(pair)
    evidence = dict(getattr(candidate, "evidence", {}) or {})
    state = evidence_state(evidence)
    plan = {
        "schema": SCHEMA,
        "plan_id": f"live-{str(candidate_id)}-{(now or _utc_now()).strftime('%Y%m%dT%H%M%SZ')}",
        "created_at": (now or _utc_now()).astimezone(timezone.utc).isoformat(),
        "candidate_id": str(candidate_id),
        "strategy_label": f"{candidate_id} {dict(getattr(candidate, 'parameters', {}) or {})}".strip(),
        "pair": str(pair),
        "broker_symbol": str(getattr(terms, "broker_symbol", "") or ""),
        "quantity": str(quantity),
        "max_open_positions": int(parsed_max_open or 0),
        "max_daily_loss_account": str(max_loss),
        "account_currency": str(
            getattr(getattr(setup, "account", None), "account_currency", "")
            or getattr(getattr(setup, "account", None), "currency", "")
            or ""
        ),
        "evidence_state": state,
        "evidence": evidence,
        "source": SOURCE,
    }
    return plan, ()


def plan_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read a stored document back, refusing anything malformed or unsafe."""

    if not isinstance(payload, Mapping):
        raise PlanError("plan_not_an_object")
    for key in _every_key(payload):
        if str(key).strip().lower() in CREDENTIAL_KEYS:
            raise PlanError("plan_contains_credential_field")
    schema = str(payload.get("schema", "") or "")
    if schema != SCHEMA:
        raise PlanError("plan_wrong_schema", schema)
    required = (
        "plan_id",
        "created_at",
        "candidate_id",
        "pair",
        "quantity",
        "max_open_positions",
        "max_daily_loss_account",
    )
    missing = [name for name in required if str(payload.get(name, "") or "").strip() == ""]
    if missing:
        raise PlanError("plan_missing_field", ",".join(missing))
    quantity = _decimal(payload.get("quantity"))
    if quantity is None or quantity <= 0:
        raise PlanError("plan_invalid_quantity", str(payload.get("quantity")))
    loss = _decimal(payload.get("max_daily_loss_account"))
    if loss is None or loss <= 0:
        raise PlanError("plan_invalid_daily_loss", str(payload.get("max_daily_loss_account")))
    try:
        max_open = int(Decimal(str(payload.get("max_open_positions"))))
    except (InvalidOperation, ValueError) as exc:
        raise PlanError("plan_invalid_max_open_positions") from exc
    if max_open < 1:
        raise PlanError("plan_invalid_max_open_positions", str(max_open))
    created = _instant(payload.get("created_at"))
    if created is None:
        raise PlanError("plan_invalid_created_at", str(payload.get("created_at")))
    document = dict(payload)
    document["quantity"] = str(quantity)
    document["max_daily_loss_account"] = str(loss)
    document["max_open_positions"] = max_open
    document["created_at"] = created.isoformat()
    return document


def load_plan(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        raise PlanError("plan_missing", str(target))
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError("plan_unreadable", type(exc).__name__) from exc
    return plan_from_payload(payload)


def write_plan(
    document: Mapping[str, Any], path: str | Path, *, overwrite: bool = False
) -> Path:
    """Write the plan atomically.  An existing plan is kept unless asked."""

    target = Path(path)
    payload = plan_from_payload(document)
    if target.exists() and not overwrite:
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        prefix=".live-plan-",
        suffix=".json",
        dir=str(target.parent),
        delete=False,
        encoding="utf-8",
    )
    try:
        handle.write(text)
        handle.close()
        temporary = Path(handle.name)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(target)
    finally:
        leftover = Path(handle.name)
        if leftover.exists():
            leftover.unlink()
    return target


def plan_view(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """The plan as an operator reads it.  No secret, no balance, no identifier."""

    if not document:
        return {"present": False}
    quantity = _decimal(document.get("quantity"))
    loss = _decimal(document.get("max_daily_loss_account"))
    return {
        "present": True,
        "plan_id": str(document.get("plan_id", "")),
        "created_at": str(document.get("created_at", "")),
        "candidate_id": str(document.get("candidate_id", "")),
        "strategy_label": str(document.get("strategy_label", "")),
        "pair": str(document.get("pair", "")),
        "broker_symbol": str(document.get("broker_symbol", "")),
        "quantity": str(quantity) if quantity is not None else "",
        "max_open_positions": int(document.get("max_open_positions", 0) or 0),
        "max_daily_loss_account": str(loss) if loss is not None else "",
        "account_currency": str(document.get("account_currency", "")),
        "evidence_state": str(document.get("evidence_state", EVIDENCE_UNVALIDATED)),
        "evidence_note": evidence_note(
            str(document.get("evidence_state", EVIDENCE_UNVALIDATED)),
            dict(document.get("evidence", {}) or {}),
        ),
        "validated": str(document.get("evidence_state", "")) == EVIDENCE_MEASURED,
    }


def describe_problem(problem: PlanProblem) -> str:
    return f"{problem.path}: {problem.message}"


def describe_problems(problems: Iterable[PlanProblem], *, limit: int = 6) -> str:
    items = list(problems)
    if not items:
        return ""
    shown = [describe_problem(item) for item in items[:limit]]
    extra = len(items) - len(shown)
    if extra > 0:
        shown.append(f"(+{extra} more)")
    return "; ".join(shown)


def _every_key(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _every_key(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _every_key(child)


__all__ = [
    "CREDENTIAL_KEYS",
    "EVIDENCE_MEASURED",
    "EVIDENCE_UNVALIDATED",
    "PLAN_FILENAME",
    "PlanError",
    "PlanProblem",
    "SCHEMA",
    "SOURCE",
    "build_plan",
    "candidate_choices",
    "describe_problem",
    "describe_problems",
    "evidence_note",
    "evidence_state",
    "load_plan",
    "plan_from_payload",
    "plan_view",
    "write_plan",
]
