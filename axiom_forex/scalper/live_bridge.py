"""Guarded AX-SCALPER typed-intent bridge into the existing /live order path.

This module owns proposal/review/approval/submission/refusal/reconciliation
state. It does **not** own a broker connection, a second send route, or a close
route. The only submission call is ``LiveLeg.execute`` (via ``leg``), which goes
through the pre-trade gate, the mode router, and the existing MT5 adapter's
write-ahead record. It never re-sends an ambiguous request: a request in doubt
is queried by the same id through the repository and reconciliation.

A real deployment has no genuine eligible evidence artifact and no signed
exact-plan approval by default. Both are re-verified immediately before each
submission, along with account, mode, plan, quote, risk, lifecycle, unresolved
orders, and broker protections. Tests may inject synthetic evidence and
ephemeral signing keys, but those fixtures are not evidence about a real market.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.audit.log import sanitize_audit_value
from app.data.models import ForexQuote, ForexQuoteBar, QuoteSideBar
from app.risk.models import FINAL_ORDER_STATES

from axiom_forex.execution.planner import ExecutionPlan, ExecutionRejection
from axiom_forex.paper_runner.live_plan import TIMEFRAME_SECONDS
from axiom_forex.paper_runner.live_workflow import ARM_CONFIRMATION_PHRASE
from axiom_forex.strategies.base import Signal

from .admission import AdmissionPolicy, load_policy
from .live_approval import (
    APPROVAL_PHRASE,
    approval_identity,
    plan_fingerprint,
    verify_approval,
)
from .live_intent import SCHEMA_V3, IntentError, ScalperLiveIntent

KIND = "AX_SCALPER_LIVE_PROPOSAL"
REVIEW_PHRASE = "REVIEW THIS EXACT LIVE PLAN"
FRAME_ALIASES: dict[str, str] = {
    "M1": "1m", "1M": "1m", "1m": "1m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "4h": "4h",
}
TERMINAL_STATES = frozenset({
    "FILLED", "PARTIAL", "REJECTED", "REFUSED", "UNRESOLVED",
    "BOOKING_FAILED", "RECONCILED",
})


class LiveIntentBridgeError(ValueError):
    """An unusable bridge configuration; callers must show a blocked state."""


class _ClosedOneShotLeg:
    """A one-shot owner-thread stack was closed; no execution is possible."""

    one_shot_ready = False

    def status(self) -> dict[str, Any]:
        return {"configured": False, "ready": False, "active": False,
                "blockers": [_blocker("one_shot_stack_closed",
                                      "The one-shot stack was closed; prepare an exact plan again.")]}

    def execute(self, _plan: Any) -> dict[str, Any]:
        raise RuntimeError("one_shot_stack_closed")

    def reconcile(self) -> dict[str, Any]:
        return {"available": False, "reconciled": False,
                "reason": "one_shot_stack_closed", "recovered": []}

    def close(self) -> None:
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _audit_view(value: Any) -> dict[str, Any]:
    method = getattr(value, "audit_view", None)
    if not callable(method):
        return {}
    try:
        return dict(method() or {})
    except Exception:
        return {}


def _blocker(reason: str, message: str) -> dict[str, str]:
    return {"reason": reason, "message": message, "hint": ""}


def _every_key(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _every_key(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _every_key(child)


CREDENTIAL_KEYS = frozenset({
    "password", "passwd", "pwd", "pass", "login", "account_login",
    "investor_password", "token", "access_token", "market_access_token",
    "api_key", "secret", "private_key",
})


@dataclass(frozen=True)
class EvidenceVerdict:
    eligible: bool
    reason: str
    evidence_digest: str = ""
    strategy: Mapping[str, Any] = None  # type: ignore[assignment]
    reviewer: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible, "reason": self.reason,
            "evidence_digest": self.evidence_digest,
            "strategy": dict(self.strategy or {}), "reviewer": self.reviewer,
        }


def blocked_evidence_gate(reason: str = "independent_replay_evidence_unavailable"):
    """A fail-closed gate for a workstation without a verified evidence host."""
    def gate(candidate_fingerprint: str, *, now: datetime | None = None) -> EvidenceVerdict:
        return EvidenceVerdict(False, reason)
    return gate


def paper_host_evidence_gate(host: Any) -> Callable[..., EvidenceVerdict]:
    """Read the PAPER host's independent signed-replay admission decision.

    This is a read of the host's authoritative status. It never promotes a PAPER
    result, never creates evidence, and never opens the PAPER host's live
    handoff. An eligible exact-fingerprint candidate is only one prerequisite.
    """
    def gate(candidate_fingerprint: str, *, now: datetime | None = None) -> EvidenceVerdict:
        try:
            status = dict(host.status() or {})
            review = dict(status.get("strategy_review") or {})
            candidates = [dict(item) for item in review.get("candidates") or ()
                          if isinstance(item, Mapping)]
            match = next((item for item in candidates
                          if str(item.get("fingerprint") or "") == candidate_fingerprint), None)
            if match is None:
                return EvidenceVerdict(False, "evidence_candidate_not_in_admission_review")
            if not bool(match.get("eligible")):
                return EvidenceVerdict(False, str(match.get("reason") or "evidence_not_admitted"))
            evidence = dict(match.get("evidence") or {})
            digest = str(evidence.get("evidence_digest") or "")
            if len(digest) != 64:
                return EvidenceVerdict(False, "evidence_digest_unavailable")
            strategy = {key: str(match.get(key) or "")
                        for key in ("name", "strategy_id", "rule_version", "fingerprint")}
            if not strategy["fingerprint"]:
                strategy["fingerprint"] = candidate_fingerprint
            return EvidenceVerdict(True, "evidence_eligible", digest, strategy,
                                   str(evidence.get("reviewer") or ""))
        except Exception as exc:
            return EvidenceVerdict(False, f"evidence_status_unreadable:{type(exc).__name__}")
    return gate


def scalper_host_policy(host: Any) -> Callable[[], AdmissionPolicy | None]:
    """Load the PAPER host's admitted reviewer policy on each exact check.

    The host, not the bridge, defines the trusted independent reviewers and the
    local PAPER currency. ``status()["config"]["currency"]`` is the connected
    account currency that ``load_live_paper_config`` has already checked against
    local simulated capital; it is not the account balance's currency field.
    An unreadable or inconsistent policy yields ``None``, so a stale key or
    missing document can only close the LIVE gate.
    """
    def loader() -> AdmissionPolicy | None:
        try:
            status = dict(host.status() or {})
            config = dict(status.get("config") or {})
            currency = str(config.get("currency") or "").strip().upper()
            path = Path(getattr(host, "admission_policy_path", "") or "")
            if not currency or not str(path):
                return None
            return load_policy(path, expected_currency=currency)
        except Exception:
            return None
    return loader


@dataclass(frozen=True)
class ExactCandidate:
    candidate_id: str
    parameters: Mapping[str, Any]
    rationale: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class RuntimeIdentity:
    account_fingerprint: str
    instrument_fingerprint: str
    risk_fingerprint: str
    config_fingerprint: str
    pair: str
    broker_symbol: str

    def audit_view(self) -> dict[str, str]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "instrument_fingerprint": self.instrument_fingerprint,
            "risk_fingerprint": self.risk_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "pair": self.pair,
            "broker_symbol": self.broker_symbol,
        }


class ScalperLiveIntentBridge:
    """One durable, fail-closed lifecycle around the existing live leg."""

    def __init__(
        self,
        *,
        repository: Any,
        live: Any,
        leg: Any,
        platform: Any,
        policy: AdmissionPolicy | None,
        approval_path: str | Path,
        evidence_gate: Callable[..., EvidenceVerdict] | None = None,
        policy_loader: Callable[[], AdmissionPolicy | None] | None = None,
        stack_loader: Callable[..., tuple[Any, Any]] | None = None,
        audit_log: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.live = live
        self.leg = leg
        self.platform = platform
        self.policy = policy
        self.policy_loader = policy_loader
        self.stack_loader = stack_loader
        self.approval_path = Path(approval_path)
        self.evidence_gate = evidence_gate or blocked_evidence_gate()
        self.audit_log = audit_log
        self.clock = clock or _utc_now
        self._stack_prepared = False
        self._stack_key = ""

    # -- durable lifecycle ------------------------------------------------

    def _snapshot_id(self, intent_id: str) -> str:
        return f"axiom-scalper-live-intent:{intent_id}"

    def _load(self, intent_id: str) -> dict[str, Any] | None:
        try:
            state = self.repository.get_snapshot(self._snapshot_id(intent_id))
        except Exception:
            return None
        if not isinstance(state, dict) or state.get("kind") != KIND:
            return None
        if str(state.get("intent", {}).get("intent_id") or "") != intent_id:
            return None
        return state

    def _write(self, proposal_state: dict[str, Any], event_type: str, **fields: Any) -> dict[str, Any]:
        intent_id = str(proposal_state.get("intent", {}).get("intent_id") or "")
        now = self.clock().astimezone(timezone.utc).isoformat()
        event = {"event_type": event_type, "at": now, "proposal_id": intent_id,
                 "live": True, **sanitize_audit_value(dict(fields))}
        record = dict(proposal_state)
        record["kind"] = KIND
        record["updated_at"] = now
        record["events"] = (list(record.get("events") or []) + [event])[-100:]
        record = sanitize_audit_value(record)
        with self.repository.transaction():
            self.repository.save_snapshot(self._snapshot_id(intent_id), KIND, record)
            self.repository.record_audit(event)
        if self.audit_log is not None:
            try:
                self.audit_log.record(event)
            except Exception:
                pass
        return record

    def _authoritative_live_view(self) -> dict[str, Any]:
        """Read shared live facts without calling LiveTrading.view() (no recursion)."""
        reader = getattr(self.live, "scalper_live_facts", None)
        if not callable(reader):
            return {"error": "live_facts_unavailable"}
        try:
            return dict(reader() or {})
        except Exception as exc:
            return {"error": type(exc).__name__}

    def _approval_policy(self) -> AdmissionPolicy | None:
        if self.policy_loader is None:
            return self.policy
        try:
            return self.policy_loader()
        except Exception:
            return None

    def _prepare_stack(self, state: Mapping[str, Any], *, prepare: bool) -> str:
        """Resolve the read-only or owner-thread exact stack, or name a refusal."""
        if self.stack_loader is None:
            return ""
        key = str(state.get("plan_fingerprint") or "")
        if not prepare and self._stack_prepared and key == self._stack_key:
            return ""
        intent, reason = self._intent_from_state(state)
        if intent is None:
            return reason
        try:
            platform, leg = self.stack_loader(state, intent, prepare=prepare)
        except Exception as exc:
            return f"one_shot_stack_unavailable:{type(exc).__name__}"
        if platform is None or leg is None:
            return "one_shot_stack_unavailable"
        self.platform = platform
        self.leg = leg
        ready = bool(getattr(leg, "one_shot_ready", False))
        # Arming is the one place the owner-thread stack must actually exist.
        # A read-only planning stack is enough for a view/review check, but it
        # must never be reported as a prepared one-shot submission surface.
        if prepare and not ready:
            self._stack_prepared = False
            self._stack_key = ""
            return "one_shot_stack_unavailable"
        self._stack_prepared = bool(prepare and ready)
        self._stack_key = key if self._stack_prepared else ""
        return ""

    def _close_one_shot(self) -> None:
        """Release the one-shot owner-thread stack after a terminal decision."""
        try:
            close = getattr(self.leg, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        try:
            release = getattr(self.stack_loader, "release", None)
            if callable(release):
                release()
        except Exception:
            pass
        if self.stack_loader is not None:
            self.leg = _ClosedOneShotLeg()
        self._stack_prepared = False
        self._stack_key = ""

    def _disarm_one_shot(self, state: dict[str, Any], *, reason: str) -> dict[str, Any]:
        """The scoped one-intent plan must not leave the general /live plan armed."""
        detail: dict[str, Any] = {
            "at": self.clock().astimezone(timezone.utc).isoformat(),
            "reason": reason,
        }
        try:
            self.live.disarm(reason=reason)
            detail["ok"] = True
        except Exception as exc:
            detail["ok"] = False
            detail["error"] = type(exc).__name__
        state["disarmed"] = detail
        try:
            return self._write(state, "AX_SCALPER_LIVE_ONE_SHOT_DISARMED", disarm=detail)
        except Exception as exc:
            detail["audit_error"] = type(exc).__name__
            state["disarmed"] = detail
            return state

    def _block(self, state: dict[str, Any] | None, reason: str, message: str,
               *, event_type: str = "AX_SCALPER_LIVE_REFUSED", **fields: Any) -> dict[str, Any]:
        if state is not None:
            terminal = str(state.get("status") or "") in TERMINAL_STATES
            blockers = list(state.get("blockers") or [])
            blockers.append(_blocker(reason, message))
            state["blockers"] = blockers[-20:]
            if not terminal:
                state["status"] = "BLOCKED"
            self._write(state, event_type, reason=reason, message=message, **fields)
        return {"ok": False, "reason": reason, "message": message,
                "blockers": [_blocker(reason, message)]}

    def _result(self, state: dict[str, Any], *, reason: str = "", message: str = "") -> dict[str, Any]:
        return {
            "ok": True, "reason": reason, "message": message,
            "intent_id": str(state.get("intent", {}).get("intent_id") or ""),
            "status": str(state.get("status") or ""),
            "plan_fingerprint": str(state.get("plan_fingerprint") or ""),
            "state": self._summary(state),
        }

    @staticmethod
    def _summary(state: Mapping[str, Any]) -> dict[str, Any]:
        intent = dict(state.get("intent") or {})
        events = [dict(item) for item in state.get("events") or () if isinstance(item, Mapping)]
        return {
            "intent_id": str(intent.get("intent_id") or ""),
            "status": str(state.get("status") or ""),
            "candidate_fingerprint": str(intent.get("candidate_fingerprint") or ""),
            "evidence_fingerprint": str(intent.get("evidence_fingerprint") or ""),
            "symbol": str(intent.get("symbol") or ""),
            "broker_symbol": str(intent.get("broker_symbol") or ""),
            "side": str(intent.get("side") or ""),
            "quantity": str(intent.get("quantity") or ""),
            "quantity_unit": str(intent.get("quantity_unit") or ""),
            "timeframe": str(intent.get("timeframe") or ""),
            "plan_fingerprint": str(state.get("plan_fingerprint") or ""),
            "reviewed": bool(state.get("operator_review")),
            "approved": bool(state.get("operator_approval")),
            "armed": bool(state.get("armed")),
            "blockers": [dict(item) for item in state.get("blockers") or ()],
            "broker": dict(state.get("broker") or {}),
            "reconciliation": dict(state.get("reconciliation") or {}),
            "last_event": dict(events[-1]) if events else {},
            "events": len(events),
            "updated_at": str(state.get("updated_at") or ""),
        }

    # -- exact runtime identity ------------------------------------------

    def _runtime_identity(self, intent: ScalperLiveIntent) -> tuple[RuntimeIdentity | None, str]:
        setup = getattr(self.platform, "setup", None)
        spec = getattr(self.platform, "spec", None)
        if setup is None or spec is None:
            return None, "provider_stack_unavailable"
        terms = setup.terms_for(intent.symbol)
        if terms is None:
            return None, "intent_pair_not_in_provider_setup"
        broker_symbol = str(getattr(terms, "broker_symbol", "") or "")
        if not broker_symbol or broker_symbol != intent.broker_symbol:
            return None, "intent_broker_symbol_mismatch"
        account = ""
        try:
            account = str(self.live.confirmed_account_fingerprint() or "")
        except Exception:
            account = ""
        if not account:
            return None, "account_fingerprint_unavailable"
        instrument = _fingerprint({
            "spec": _audit_view(spec),
            "terms": {
                "pair": terms.pair, "broker_symbol": broker_symbol,
                "suffix": terms.suffix, "pip_size": str(terms.pip_size),
                "calendar_id": terms.calendar_id,
                "calendar_version": terms.calendar_version,
                "correlation_group": terms.correlation_group,
                "margin_multiplier": str(terms.margin_multiplier),
                "spec_version": terms.spec_version, "source": terms.source,
            },
        })
        risk = _fingerprint({
            "risk": _audit_view(getattr(setup, "risk", None)),
            "intent": {
                "stop_price": str(intent.stop_price),
                "target_price": str(intent.target_price),
                "quantity": str(intent.quantity),
                "quantity_unit": intent.quantity_unit,
            },
        })
        setup_view = {key: value for key, value in _audit_view(setup).items()
                      if key != "path"}
        config = _fingerprint({
            "setup": setup_view,
            "costs": _audit_view(getattr(setup, "costs", None)),
            "rollover": _audit_view(getattr(setup, "rollover", None)),
            "calendar": _audit_view(getattr(setup, "calendar", None)),
            "quote_freshness": _audit_view(getattr(self.platform, "quote_freshness", None)),
            "execution_config": _audit_view(getattr(self.platform, "execution_config", None)),
            "planner": _audit_view(getattr(self.platform, "planner", None)),
        })
        return RuntimeIdentity(
            account_fingerprint=account, instrument_fingerprint=instrument,
            risk_fingerprint=risk, config_fingerprint=config,
            pair=str(getattr(spec, "canonical_pair", "") or terms.pair),
            broker_symbol=broker_symbol,
        ), ""

    def _evidence(self, intent: ScalperLiveIntent, *, now: datetime) -> tuple[EvidenceVerdict | None, str]:
        try:
            verdict = self.evidence_gate(intent.candidate_fingerprint, now=now)
        except Exception as exc:
            return None, f"evidence_gate_unreadable:{type(exc).__name__}"
        if not isinstance(verdict, EvidenceVerdict):
            return None, "evidence_gate_invalid_result"
        if not verdict.eligible:
            return verdict, f"evidence_not_eligible:{verdict.reason}"
        if str(intent.evidence_fingerprint) != verdict.evidence_digest:
            return verdict, "intent_evidence_digest_mismatch"
        return verdict, ""

    def _candidate(self, intent: ScalperLiveIntent, verdict: EvidenceVerdict) -> ExactCandidate:
        strategy = dict(verdict.strategy or {})
        return ExactCandidate(
            candidate_id=intent.candidate_fingerprint,
            parameters={"rule_version": strategy.get("rule_version", ""),
                        "fingerprint": strategy.get("fingerprint", intent.candidate_fingerprint)},
            rationale="One exact strategy fingerprint bound to independently signed replay evidence.",
            evidence={
                "evaluated": True, "passed": True,
                "data_class": "SIGNED_INDEPENDENT_REPLAY",
                "dataset_id": verdict.evidence_digest[:12],
                "criteria_version": "axiom-scalper-admission-policy-v1",
                "note": "Signed replay met the declared policy. This is not a forecast or a profit claim.",
            },
        )

    def _ensure_candidate(self, candidate: ExactCandidate) -> None:
        existing = tuple(getattr(self.live, "candidates", ()) or ())
        kept = tuple(item for item in existing
                     if str(getattr(item, "candidate_id", "")) != candidate.candidate_id)
        self.live.candidates = kept + (candidate,)

    def _plan_values(self, intent: ScalperLiveIntent, identity: RuntimeIdentity) -> tuple[dict[str, str] | None, str]:
        setup = getattr(self.platform, "setup", None)
        spec = getattr(self.platform, "spec", None)
        if setup is None or spec is None:
            return None, "provider_stack_unavailable"
        terms = setup.terms_for(intent.symbol)
        if terms is None:
            return None, "intent_pair_not_in_provider_setup"
        normalized = FRAME_ALIASES.get(intent.timeframe)
        if normalized is None or normalized not in TIMEFRAME_SECONDS:
            return None, "intent_timeframe_not_supported"
        pip_size = _decimal(getattr(terms, "pip_size", None))
        if pip_size is None or pip_size <= 0:
            return None, "pip_size_unavailable"
        entry = intent.quote_ask if intent.side == "BUY" else intent.quote_bid
        distance = (entry - intent.stop_price) if intent.side == "BUY" else (intent.stop_price - entry)
        if distance <= 0:
            return None, "protective_stop_direction_invalid"
        protective = distance / pip_size
        if protective <= 0:
            return None, "protective_stop_unmeasurable"
        quantity = intent.quantity
        if intent.quantity_unit == "base_units":
            contract = _decimal(getattr(spec, "contract_size", None))
            if contract is None or contract <= 0:
                return None, "contract_size_unavailable"
            quantity = intent.quantity / contract
            step = _decimal(getattr(spec, "quantity_step", None))
            minimum = _decimal(getattr(spec, "min_quantity", None))
            if step is not None and step > 0 and minimum is not None:
                steps = (quantity - minimum) / step
                if steps != steps.to_integral_value():
                    return None, "base_units_do_not_map_to_broker_volume_grid"
        elif intent.quantity_unit != "lots":
            return None, "declared_quantity_unit_invalid"
        if quantity <= 0:
            return None, "quantity_conversion_invalid"
        max_open = getattr(getattr(setup, "risk", None), "max_open_positions", None)
        max_loss = _decimal(getattr(getattr(setup, "risk", None), "max_daily_loss_account", None))
        if not isinstance(max_open, int) or max_open < 1 or max_loss is None or max_loss <= 0:
            return None, "risk_limits_unavailable"
        payload = intent.as_payload()
        bindings = {
            "intent_id": intent.intent_id,
            "candidate_fingerprint": intent.candidate_fingerprint,
            "evidence_fingerprint": intent.evidence_fingerprint,
            "account_fingerprint": identity.account_fingerprint,
            "instrument_fingerprint": identity.instrument_fingerprint,
            "risk_fingerprint": identity.risk_fingerprint,
            "config_fingerprint": identity.config_fingerprint,
            "broker_symbol": intent.broker_symbol,
            "declared_quantity": str(intent.quantity),
            "declared_quantity_unit": intent.quantity_unit,
            "quantity_unit": intent.quantity_unit,
            "stop_price": str(intent.stop_price),
            "target_price": str(intent.target_price),
            "quote_bid": str(intent.quote_bid),
            "quote_ask": str(intent.quote_ask),
            "quote_timestamp": payload["quote_timestamp"],
            "quote_received_at": payload["quote_received_at"],
            "quote_source": intent.quote_source,
            "expires_at": payload["expires_at"],
            "intent_schema": intent.schema,
            "intent_timeframe": intent.timeframe,
            "side": intent.side,
        }
        values = {
            "candidate_id": intent.candidate_fingerprint,
            "pair": intent.symbol,
            "timeframe": normalized,
            "quantity": str(quantity),
            "max_open_positions": str(max_open),
            "max_daily_loss_account": str(max_loss),
            "protective_stop_pips": str(protective),
        }
        values.update({f"scalper_{key}": str(value) for key, value in bindings.items()})
        return values, ""

    def _plan(self, intent: ScalperLiveIntent, *, now: datetime,
              verdict: EvidenceVerdict, identity: RuntimeIdentity):
        values, reason = self._plan_values(intent, identity)
        if values is None:
            return None, reason
        candidate = self._candidate(intent, verdict)
        try:
            document, problems = self.live.build(values, candidates=(candidate,))
        except Exception as exc:
            return None, f"exact_plan_build_failed:{type(exc).__name__}"
        if problems or document is None:
            detail = "; ".join(f"{item.path}: {item.message}" for item in problems)
            return None, f"exact_plan_invalid:{detail}"
        fingerprint = plan_fingerprint(intent=intent.as_payload(), plan=document,
                                       evidence_digest=verdict.evidence_digest)
        return {"values": values, "document": document,
                "fingerprint": fingerprint, "candidate": candidate}, ""

    def _intent_from_state(self, state: Mapping[str, Any]) -> tuple[ScalperLiveIntent | None, str]:
        raw = dict(state.get("intent") or {})
        try:
            return ScalperLiveIntent.parse(raw, now=self.clock()), ""
        except IntentError as exc:
            return None, f"stored_intent_invalid:{exc}"
        except Exception as exc:
            return None, f"stored_intent_unreadable:{type(exc).__name__}"

    # -- operator workflow ------------------------------------------------

    def register(self, raw: Mapping[str, Any] | str) -> dict[str, Any]:
        """Persist a typed proposal and its exact /live plan, or a named refusal."""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            return {"ok": False, "reason": "intent_json_invalid",
                    "message": "The proposal must be one JSON object.", "blockers": []}
        if not isinstance(payload, Mapping):
            return {"ok": False, "reason": "intent_not_an_object",
                    "message": "The proposal must be one JSON object.", "blockers": []}
        if any(str(key).strip().lower() in CREDENTIAL_KEYS for key in _every_key(payload)):
            return {"ok": False, "reason": "intent_contains_credential_field",
                    "message": "A typed proposal must not carry credentials.", "blockers": []}
        try:
            intent = ScalperLiveIntent.parse(payload, now=self.clock())
        except IntentError as exc:
            return {"ok": False, "reason": f"intent_invalid:{exc}",
                    "message": "The typed proposal was refused; no plan was created.", "blockers": []}
        if intent.schema != SCHEMA_V3:
            return {"ok": False, "reason": "intent_schema_not_live_ready",
                    "message": "Only v3 proposals with an exact broker symbol and receive time enter /live.",
                    "blockers": []}
        state: dict[str, Any] = {
            "schema": KIND, "status": "RECEIVED", "intent": intent.as_payload(),
            "evidence": {}, "identity": {}, "values": {}, "plan": {},
            "plan_fingerprint": "", "operator_review": {}, "operator_approval": {},
            "signed_approval": {}, "armed": {}, "submission": {}, "broker": {},
            "reconciliation": {}, "blockers": [], "events": [],
        }
        now = self.clock()
        verdict, reason = self._evidence(intent, now=now)
        if verdict is None or reason:
            state["evidence"] = verdict.audit_view() if verdict else {"reason": reason}
            return self._block(state, reason or "evidence_unavailable",
                               "Independent signed replay evidence is not eligible for this exact candidate.")
        identity, reason = self._runtime_identity(intent)
        if identity is None or reason:
            return self._block(state, reason or "runtime_identity_unavailable",
                               "The exact account, instrument, risk and configuration identity could not be confirmed.")
        if not intent.matches(
            candidate_fingerprint=intent.candidate_fingerprint,
            evidence_fingerprint=verdict.evidence_digest,
            account_fingerprint=identity.account_fingerprint,
            instrument_fingerprint=identity.instrument_fingerprint,
            symbol=intent.symbol, timeframe=intent.timeframe,
            risk_fingerprint=identity.risk_fingerprint,
            config_fingerprint=identity.config_fingerprint,
            quantity_unit=intent.quantity_unit,
            broker_symbol=identity.broker_symbol,
            quote_received_at=intent.quote_received_at,
        ):
            return self._block(state, "intent_runtime_identity_mismatch",
                               "Exact account, instrument, risk, configuration, broker symbol or receive time differs.")
        self._ensure_candidate(self._candidate(intent, verdict))
        built, reason = self._plan(intent, now=now, verdict=verdict, identity=identity)
        if built is None:
            return self._block(state, reason, "The exact /live plan could not be built from declared values.")
        state.update({
            "status": "RECEIVED", "evidence": verdict.audit_view(),
            "identity": identity.audit_view(), "values": built["values"],
            "plan": built["document"], "plan_fingerprint": built["fingerprint"],
            "blockers": [],
        })
        state = self._write(state, "AX_SCALPER_LIVE_PROPOSAL_RECEIVED",
                            plan_fingerprint=built["fingerprint"],
                            evidence_digest=verdict.evidence_digest)
        return self._result(state, reason="proposal_received",
                            message="Exact proposal recorded. Review and approval are separate actions; nothing was armed or submitted.")

    def review(self, intent_id: str, *, reviewed: bool, phrase: str,
               account_confirmation: str) -> dict[str, Any]:
        state = self._load(intent_id)
        if state is None:
            return {"ok": False, "reason": "proposal_missing", "message": "No such proposal is stored.", "blockers": []}
        if str(state.get("status")) not in {"RECEIVED", "REVIEWED", "BLOCKED"}:
            return self._block(state, "proposal_not_reviewable", "This proposal has passed review and cannot be reviewed again.")
        if not reviewed or str(phrase or "").strip().upper() != REVIEW_PHRASE:
            return self._block(state, "review_confirmation_mismatch", f"Tick the review box and type {REVIEW_PHRASE!r} exactly.")
        expected_account = str(state.get("identity", {}).get("account_fingerprint") or "")
        if not expected_account or str(account_confirmation or "").strip() != expected_account:
            return self._block(state, "review_account_confirmation_mismatch", "Repeat the exact account fingerprint shown on /live.")
        exact, reason = self._exact_checks(state, now=self.clock(), stage="review")
        if reason:
            return self._block(state, reason, "The exact proposal no longer matches current evidence, identity or plan.")
        state["status"] = "REVIEWED"
        state["operator_review"] = {
            "at": self.clock().isoformat(), "phrase": REVIEW_PHRASE,
            "account_fingerprint": expected_account,
            "plan_fingerprint": state.get("plan_fingerprint", ""),
            "evidence": exact["evidence"].audit_view(),
        }
        state["blockers"] = []
        state = self._write(state, "AX_SCALPER_LIVE_PROPOSAL_REVIEWED",
                            plan_fingerprint=state.get("plan_fingerprint", ""))
        return self._result(state, reason="proposal_reviewed",
                            message="Exact candidate, evidence and plan reviewed. A separate approval and arming are still required.")

    def approve(self, intent_id: str, *, plan_fingerprint_value: str, phrase: str,
                account_confirmation: str) -> dict[str, Any]:
        state = self._load(intent_id)
        if state is None:
            return {"ok": False, "reason": "proposal_missing", "message": "No such proposal is stored.", "blockers": []}
        if str(state.get("status")) != "REVIEWED":
            return self._block(state, "proposal_not_in_reviewed_state", "Review the exact proposal before approving it.")
        if str(phrase or "").strip().upper() != APPROVAL_PHRASE:
            return self._block(state, "operator_approval_phrase_mismatch", f"Type {APPROVAL_PHRASE!r} exactly.")
        if str(plan_fingerprint_value or "").strip() != str(state.get("plan_fingerprint") or ""):
            return self._block(state, "operator_plan_fingerprint_mismatch", "Repeat the exact plan fingerprint shown on /live.")
        expected_account = str(state.get("identity", {}).get("account_fingerprint") or "")
        if str(account_confirmation or "").strip() != expected_account:
            return self._block(state, "operator_account_confirmation_mismatch", "Repeat the exact account fingerprint shown on /live.")
        exact, reason = self._exact_checks(state, now=self.clock(), stage="approve")
        if reason:
            return self._block(state, reason, "The exact proposal no longer matches current evidence, identity or plan.")
        policy = self._approval_policy()
        if policy is None:
            return self._block(state, "approval_policy_missing", "No trusted reviewer policy is configured.")
        approval = verify_approval(
            self.approval_path, policy=policy,
            expected_fingerprint=str(state.get("plan_fingerprint") or ""),
            expected_identity=approval_identity(dict(state.get("intent") or {}),
                                                evidence_digest=exact["evidence"].evidence_digest),
            now=self.clock(),
        )
        if not approval.valid:
            return self._block(state, f"signed_approval_invalid:{approval.reason}",
                               "A separately signed exact-plan LIVE approval is required and was not verified.")
        state["status"] = "APPROVED"
        state["operator_approval"] = {
            "at": self.clock().isoformat(), "phrase": APPROVAL_PHRASE,
            "plan_fingerprint": state.get("plan_fingerprint", ""),
            "account_fingerprint": expected_account,
        }
        state["signed_approval"] = approval.audit_view()
        state["blockers"] = []
        state = self._write(state, "AX_SCALPER_LIVE_PLAN_APPROVED",
                            approval_fingerprint=approval.fingerprint,
                            reviewer=approval.reviewer)
        return self._result(state, reason="proposal_approved",
                            message="Separate operator and signed exact-plan approvals recorded. Nothing was armed or submitted.")

    def arm(self, intent_id: str, *, confirmation: str,
            account_confirmation: str) -> dict[str, Any]:
        state = self._load(intent_id)
        if state is None:
            return {"ok": False, "reason": "proposal_missing", "message": "No such proposal is stored.", "blockers": []}
        if str(state.get("status")) != "APPROVED":
            return self._block(state, "proposal_not_approved", "Only a separately approved exact plan can be armed.")
        if str(confirmation or "").strip().upper() != ARM_CONFIRMATION_PHRASE:
            return self._block(state, "arm_confirmation_mismatch", f"Type {ARM_CONFIRMATION_PHRASE!r} exactly.")
        expected_account = str(state.get("identity", {}).get("account_fingerprint") or "")
        if str(account_confirmation or "").strip() != expected_account:
            return self._block(state, "arm_account_confirmation_mismatch", "Repeat the exact account fingerprint shown on /live.")
        exact, reason = self._exact_checks(state, now=self.clock(), stage="arm")
        if reason:
            return self._block(state, reason, "The exact proposal no longer matches current evidence, identity or plan.")
        if bool(getattr(self.live, "armed", False)):
            return self._block(state, "live_already_armed", "Disarm the existing /live plan before arming this exact proposal.")
        candidate = self._candidate(exact["intent"], exact["evidence"])
        self._ensure_candidate(candidate)
        try:
            result = self.live.arm(
                dict(state.get("values") or {}), confirmation=ARM_CONFIRMATION_PHRASE,
                reviewed=True, account_confirmation=expected_account,
                candidates=(candidate,),
            )
        except Exception as exc:
            return self._block(state, f"arm_failed:{type(exc).__name__}", "The existing /live workflow refused to arm this plan.")
        if not bool(getattr(result, "armed", False)):
            blockers = [item.audit_view() for item in getattr(result, "blockers", ()) or ()]
            return self._block(state, str(getattr(result, "reason") or "arm_refused"),
                               str(getattr(result, "message") or "The existing /live workflow refused to arm."),
                               arm_blockers=blockers)
        try:
            armed_plan = dict(getattr(self.live, "plan", {}) or {})
            armed_fingerprint = plan_fingerprint(
                intent=exact["intent"].as_payload(), plan=armed_plan,
                evidence_digest=exact["evidence"].evidence_digest,
            )
        except Exception as exc:
            armed_fingerprint = ""
        if armed_fingerprint != str(state.get("plan_fingerprint") or ""):
            try:
                self.live.disarm(reason="scalper_exact_plan_mismatch")
            except Exception:
                pass
            self._close_one_shot()
            return self._block(state, "armed_plan_identity_mismatch",
                               "The armed plan is not the exact reviewed plan; live trading was disarmed.")
        stack_reason = self._prepare_stack(state, prepare=True)
        if stack_reason:
            try:
                self.live.disarm(reason="scalper_one_shot_stack_unavailable")
            except Exception:
                pass
            self._close_one_shot()
            return self._block(state, stack_reason,
                               "The exact one-shot owner-thread stack was not prepared; live trading was disarmed.")
        state["status"] = "ARMED"
        state["armed"] = {
            "at": self.clock().isoformat(), "account_fingerprint": expected_account,
            "plan_fingerprint": armed_fingerprint,
        }
        state["blockers"] = []
        state = self._write(state, "AX_SCALPER_LIVE_PLAN_ARMED",
                            plan_fingerprint=armed_fingerprint)
        return self._result(state, reason="proposal_armed",
                            message="Exact plan armed through the existing /live workflow. No order has been submitted.")

    # -- exact checks and submission -------------------------------------

    def _exact_checks(self, state: Mapping[str, Any], *, now: datetime, stage: str):
        intent, reason = self._intent_from_state(state)
        if intent is None:
            return {}, reason
        reason = self._prepare_stack(state, prepare=stage == "submit")
        if reason:
            return {}, reason
        verdict, reason = self._evidence(intent, now=now)
        if verdict is None or reason:
            return {}, reason or "evidence_unavailable"
        identity, reason = self._runtime_identity(intent)
        if identity is None or reason:
            return {}, reason or "runtime_identity_unavailable"
        if not intent.matches(
            candidate_fingerprint=intent.candidate_fingerprint,
            evidence_fingerprint=verdict.evidence_digest,
            account_fingerprint=identity.account_fingerprint,
            instrument_fingerprint=identity.instrument_fingerprint,
            symbol=intent.symbol, timeframe=intent.timeframe,
            risk_fingerprint=identity.risk_fingerprint,
            config_fingerprint=identity.config_fingerprint,
            quantity_unit=intent.quantity_unit,
            broker_symbol=identity.broker_symbol,
            quote_received_at=intent.quote_received_at,
        ):
            return {}, "intent_runtime_identity_mismatch"
        values, reason = self._plan_values(intent, identity)
        if values is None:
            return {}, reason
        if dict(state.get("values") or {}) != values:
            return {}, "stored_plan_values_changed"
        plan, reason = self._plan(intent, now=now, verdict=verdict, identity=identity)
        if plan is None:
            return {}, reason
        if str(plan["fingerprint"]) != str(state.get("plan_fingerprint") or ""):
            return {}, "plan_fingerprint_changed"
        if stage in {"approve", "arm", "submit", "view"}:
            policy = self._approval_policy()
            if policy is None:
                return {}, "approval_policy_missing"
            approval = verify_approval(
                self.approval_path, policy=policy,
                expected_fingerprint=str(state.get("plan_fingerprint") or ""),
                expected_identity=approval_identity(intent.as_payload(),
                                                    evidence_digest=verdict.evidence_digest),
                now=now,
            )
            if not approval.valid:
                return {}, f"signed_approval_invalid:{approval.reason}"
        return {"intent": intent, "evidence": verdict, "identity": identity,
                "plan": plan, "values": values}, ""

    def _live_blockers(self, *, intent_id: str, live_view: Mapping[str, Any],
                       leg_status: Mapping[str, Any]) -> list[dict[str, str]]:
        blockers: list[dict[str, str]] = []
        if live_view.get("error"):
            blockers.append(_blocker("live_view_unreadable", str(live_view["error"])))
        if leg_status.get("error"):
            blockers.append(_blocker("leg_status_unreadable", str(leg_status["error"])))
        if live_view.get("session_running") or live_view.get("next_signal_sends_real_order"):
            blockers.append(_blocker("continuous_live_session_running",
                                     "Stop the continuous /live session before any one-shot typed intent."))
        if not live_view.get("connected"):
            blockers.append(_blocker("mt5_not_connected", "The terminal is not connected."))
        if str(live_view.get("mode") or "").upper() != "LIVE" or not live_view.get("live_effective"):
            blockers.append(_blocker("mode_not_live_confirmed", "LIVE is not selected and confirmed in this process."))
        if not live_view.get("armed"):
            blockers.append(_blocker("live_not_armed", "The exact /live plan is not armed."))
        for item in live_view.get("blockers") or ():
            if isinstance(item, Mapping):
                blockers.append(_blocker(str(item.get("reason") or "live_blocker"),
                                         str(item.get("message") or "")))
        for item in leg_status.get("blockers") or ():
            if isinstance(item, Mapping):
                blockers.append(_blocker(str(item.get("reason") or "leg_blocker"),
                                         str(item.get("message") or "")))
        try:
            unresolved = self.repository.unresolved_order_ids(final_states=FINAL_ORDER_STATES)
        except Exception as exc:
            blockers.append(_blocker("order_ledger_unreadable", type(exc).__name__))
        else:
            if unresolved:
                blockers.append(_blocker("unresolved_order_present",
                                         "An earlier broker order is in doubt; query by the same identity, never resend."))
        try:
            existing = self.repository.get_order(intent_id)
        except Exception as exc:
            blockers.append(_blocker("order_record_unreadable", type(exc).__name__))
        else:
            if existing is not None:
                blockers.append(_blocker("duplicate_request", "The exact request identity already has a durable broker order record."))
        return blockers

    def _submission_blockers(self, state: Mapping[str, Any], *, now: datetime,
                             live_view: Mapping[str, Any] | None = None,
                             leg_status: Mapping[str, Any] | None = None,
                             stage: str = "submit") -> tuple[list[dict[str, str]], dict[str, Any]]:
        if str(state.get("status")) == "SUBMITTING":
            return [_blocker("submission_in_doubt", "The prior submission is in doubt; query by the same identity.")], {}
        if str(state.get("status")) in TERMINAL_STATES:
            return [_blocker("proposal_already_submitted", "This exact proposal already has a terminal broker outcome.")], {}
        if str(state.get("status")) != "ARMED":
            return [_blocker("proposal_not_armed", "Review, approve and arm the exact plan first.")], {}
        exact, reason = self._exact_checks(state, now=now, stage=stage)
        if reason:
            return [_blocker(reason, "An exact evidence, approval, identity or plan check failed.")], {}
        intent: ScalperLiveIntent = exact["intent"]
        view = dict(live_view) if live_view is not None else self._authoritative_live_view()
        try:
            status = dict(leg_status) if leg_status is not None else dict(self.leg.status() or {})
        except Exception as exc:
            status = {"error": type(exc).__name__}
        blockers = self._live_blockers(intent_id=intent.intent_id, live_view=view, leg_status=status)
        try:
            armed_plan = dict(getattr(self.live, "plan", {}) or {})
            fingerprint = plan_fingerprint(intent=intent.as_payload(), plan=armed_plan,
                                           evidence_digest=exact["evidence"].evidence_digest)
            if fingerprint != str(state.get("plan_fingerprint") or ""):
                blockers.append(_blocker("armed_plan_identity_mismatch", "The armed plan no longer matches the approved exact plan."))
            bindings = dict(armed_plan.get("scalper_live") or {})
            expected_bindings = {key: str(state["values"].get(f"scalper_{key}", "") or "")
                                 for key in bindings}
            if bindings and dict(bindings) != expected_bindings:
                blockers.append(_blocker("armed_plan_bindings_mismatch", "The armed typed-intent identity was changed."))
        except Exception as exc:
            blockers.append(_blocker("armed_plan_unreadable", type(exc).__name__))
        freshness = getattr(self.platform, "quote_freshness", None)
        max_age = _decimal(getattr(freshness, "max_age_seconds", None))
        max_latency = _decimal(getattr(freshness, "max_feed_latency_seconds", None))
        if max_age is None or max_latency is None:
            blockers.append(_blocker("quote_freshness_policy_unavailable", "No explicit quote-age and feed-latency policy is configured."))
        else:
            if not intent.quote_is_fresh(now=now, max_age_seconds=max_age):
                blockers.append(_blocker("stale_quote", "The exact typed quote is older than the declared maximum age."))
            latency = intent.feed_latency_seconds()
            if latency is None or latency > max_latency:
                blockers.append(_blocker("feed_latency_exceeded", "The declared quote receive latency is unavailable or too large."))
        if intent.expires_at <= now:
            blockers.append(_blocker("intent_expired", "The typed proposal has expired."))
        return blockers, {
            "intent": intent,
            "evidence": exact["evidence"],
            "exact": exact,
            # Read after _exact_checks may have resolved a different exact stack.
            "leg_status": status,
        }

    def _execution_plan(self, intent: ScalperLiveIntent) -> tuple[ExecutionPlan | None, str]:
        spec = getattr(self.platform, "spec", None)
        planner = getattr(self.platform, "planner", None)
        if spec is None or planner is None:
            return None, "provider_stack_unavailable"
        normalized = FRAME_ALIASES.get(intent.timeframe, "")
        seconds = TIMEFRAME_SECONDS.get(normalized, 0)
        if seconds <= 0:
            return None, "intent_timeframe_not_supported"
        quote_at = intent.quote_timestamp
        received = intent.quote_received_at or quote_at
        quote = ForexQuote(
            pair=str(getattr(spec, "canonical_pair", "") or intent.symbol),
            timestamp=quote_at, bid=intent.quote_bid, ask=intent.quote_ask,
            source=intent.quote_source, quote_id=intent.intent_id,
            received_at=received,
            metadata={"intent_id": intent.intent_id, "basis": "AXIOM_TYPED_INTENT_QUOTE"},
        )
        bid_bar = QuoteSideBar(open=intent.quote_bid, high=intent.quote_bid,
                               low=intent.quote_bid, close=intent.quote_bid)
        ask_bar = QuoteSideBar(open=intent.quote_ask, high=intent.quote_ask,
                               low=intent.quote_ask, close=intent.quote_ask)
        bar = ForexQuoteBar(
            pair=quote.pair, timeframe=normalized,
            timestamp=quote_at - timedelta(seconds=seconds),
            duration_seconds=seconds, bid=bid_bar, ask=ask_bar,
            source="AXIOM_TYPED_INTENT_SINGLE_QUOTE_CONTEXT",
            quote_count=1, last_quote_at=quote_at - timedelta(milliseconds=1),
            complete=True,
        )
        validation = bar.validate(spec)
        if not validation.valid:
            return None, f"decision_bar_invalid:{validation.reason}"
        signal = Signal.BUY if intent.side == "BUY" else Signal.SELL
        planned = planner.plan_at_quote(
            signal=signal, positions={}, quote=quote, decision_bar=bar,
            decision_index=0, run_id=intent.intent_id,
            strategy_id=intent.candidate_fingerprint,
        )
        if planned is None:
            return None, "no_action_for_exact_intent"
        if isinstance(planned, ExecutionRejection):
            return None, f"planner_refused:{planned.code}:{planned.reason}"
        if not isinstance(planned, ExecutionPlan):
            return None, "planner_result_invalid"
        # The typed intent's own id is the single idempotency identity. The
        # planner's sequence-derived id is replaced, never used as a second key.
        request = replace(planned.request, request_id=intent.intent_id)
        return replace(planned, request=request), ""

    def _refuse_one_shot(self, state: dict[str, Any], reason: str, message: str,
                         blockers: Sequence[Mapping[str, Any]], *, now: datetime) -> dict[str, Any]:
        """No broker request was sent; make the armed one-shot plan inert."""
        intent_id = str(state.get("intent", {}).get("intent_id") or "")
        saved_blockers = [dict(item) for item in blockers]
        state["status"] = "REFUSED"
        state["blockers"] = saved_blockers
        state["submission"] = {
            "at": now.isoformat(), "request_id": intent_id,
            "submitted": False, "reason": reason,
        }
        state = self._write(state, "AX_SCALPER_LIVE_SUBMISSION_REFUSED",
                            reason=reason, request_id=intent_id,
                            submission_blockers=saved_blockers)
        state = self._disarm_one_shot(state, reason="scalper_submission_refused")
        self._close_one_shot()
        result = self._block(state, reason, message, submission_blockers=saved_blockers)
        result["blockers"] = saved_blockers
        return result

    def submit(self, intent_id: str) -> dict[str, Any]:
        state = self._load(intent_id)
        if state is None:
            return {"ok": False, "reason": "proposal_missing", "message": "No such proposal is stored.", "blockers": []}
        now = self.clock()
        blockers, exact = self._submission_blockers(state, now=now)
        if blockers:
            reason = blockers[0]["reason"]
            if reason in {"submission_in_doubt", "duplicate_request", "proposal_already_submitted",
                          "unresolved_order_present", "order_ledger_unreadable", "order_record_unreadable"}:
                return self._query_same_identity(state, reason=reason)
            return self._refuse_one_shot(state, reason, blockers[0]["message"], blockers, now=now)
        intent: ScalperLiveIntent = exact["intent"]
        execution, reason = self._execution_plan(intent)
        if execution is None:
            return self._refuse_one_shot(
                state, reason, "The exact typed quote could not become one gate-approved order.",
                [_blocker(reason, "No order was created or sent.")], now=now,
            )
        state["status"] = "SUBMITTING"
        state["submission"] = {
            "at": now.isoformat(), "request_id": intent.intent_id,
            "intent_fingerprint": str(state.get("plan_fingerprint") or ""),
            "basis": "AXIOM_TYPED_INTENT_SINGLE_QUOTE_CONTEXT",
        }
        state = self._write(state, "AX_SCALPER_LIVE_SUBMISSION_STARTED",
                            request_id=intent.intent_id)
        try:
            outcome = dict(self.leg.execute(execution) or {})
        except Exception as exc:
            state = self._write(state, "AX_SCALPER_LIVE_SUBMISSION_UNCERTAIN",
                                reason=type(exc).__name__, request_id=intent.intent_id)
            return self._query_same_identity(state, reason="submission_outcome_unreadable")
        filled = _decimal(outcome.get("volume_filled")) or Decimal(0)
        requested = _decimal(outcome.get("volume_requested")) or Decimal(0)
        submitted = bool(outcome.get("submitted"))
        unknown = bool(outcome.get("unknown")) or str(outcome.get("state") or "") == "UNRESOLVED"
        if not submitted:
            final = "REFUSED"
        elif unknown:
            final = "UNRESOLVED"
        elif str(outcome.get("state") or "").upper() == "REJECTED" or \
                "REJECT" in str(outcome.get("classification") or "").upper() or filled <= 0:
            final = "REJECTED"
        elif not outcome.get("booked"):
            final = "BOOKING_FAILED"
        elif requested > 0 and filled < requested:
            final = "PARTIAL"
        else:
            final = "FILLED"
        state["status"] = final
        state["broker"] = outcome
        state["blockers"] = []
        state = self._write(state, "AX_SCALPER_LIVE_BROKER_RESPONSE",
                            request_id=intent.intent_id, broker_state=final, outcome=outcome)
        state = self.reconcile(intent_id, state=state, event_reason=final)
        state = self._disarm_one_shot(state, reason=f"scalper_one_shot_{final.lower()}")
        self._close_one_shot()
        return {
            "ok": final in {"FILLED", "PARTIAL"},
            "reason": final,
            "message": "The existing live leg returned one broker outcome; it was recorded and reconciled.",
            "broker": dict(outcome),
            "status": str(state.get("status") or final),
            "state": self._summary(state),
            "blockers": [],
        }

    def _query_same_identity(self, state: dict[str, Any], *, reason: str) -> dict[str, Any]:
        """Never resend: read the durable order and the live leg's reconciliation."""
        intent_id = str(state.get("intent", {}).get("intent_id") or "")
        try:
            order = self.repository.get_order(intent_id)
        except Exception:
            order = None
        state = self._disarm_one_shot(state, reason="scalper_ambiguous_or_duplicate_submission")
        reconciliation = self.reconcile(intent_id, state=state, event_reason=reason)
        self._close_one_shot()
        return {
            "ok": False, "reason": reason,
            "message": "No order was resent. The same request identity was queried in the durable ledger and broker reconciliation.",
            "order_record": dict(order or {}),
            "state": self._summary(reconciliation),
            "blockers": [_blocker(reason, "Ambiguous or duplicate requests are queried, never re-sent.")],
        }

    def reconcile(self, intent_id: str, *, state: dict[str, Any] | None = None,
                  event_reason: str = "operator_reconcile") -> dict[str, Any]:
        state = state or self._load(intent_id)
        if state is None:
            return {"ok": False, "reason": "proposal_missing", "message": "No such proposal is stored."}
        try:
            result = dict(self.leg.reconcile() or {})
        except Exception as exc:
            result = {"available": False, "reason": type(exc).__name__}
        try:
            order = self.repository.get_order(intent_id)
        except Exception:
            order = None
        state["reconciliation"] = {
            "at": self.clock().isoformat(), "result": result,
            "order_record": dict(order or {}), "request_id": intent_id,
        }
        if str(state.get("status")) in {"SUBMITTING", "UNRESOLVED"} and result.get("reconciled"):
            record_state = str((order or {}).get("state") or "").upper()
            if record_state in {"FILLED", "REJECTED", "CANCELLED"}:
                state["status"] = "RECONCILED"
        state = self._write(state, "AX_SCALPER_LIVE_RECONCILED",
                            reason=event_reason, request_id=intent_id,
                            reconciled=bool(result.get("reconciled")))
        return state

    # -- authoritative read model ----------------------------------------

    def view(self, *, intent_id: str = "", live_view: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Read model. A host may supply its own authoritative facts to avoid recursion."""
        try:
            states = [dict(item) for item in self.repository.list_snapshots(kind=KIND)
                      if isinstance(item, Mapping)]
        except Exception as exc:
            return {"available": True, "submission_available": False,
                    "error": type(exc).__name__, "proposals": [], "blockers":
                    [_blocker("proposal_store_unreadable", "The durable proposal store could not be read.")]}
        states.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        selected = next((item for item in states
                         if str(item.get("intent", {}).get("intent_id") or "") == intent_id), None) if intent_id else None
        if selected is None:
            selected = next((item for item in states if str(item.get("status")) in {"ARMED", "APPROVED", "REVIEWED"}), None)
        if selected is None and states:
            selected = states[0]
        facts: dict[str, Any] = dict(live_view) if live_view is not None else self._authoritative_live_view()

        def read_leg_status() -> dict[str, Any]:
            try:
                return dict(self.leg.status() or {})
            except Exception as exc:
                return {"error": type(exc).__name__}

        blockers: list[dict[str, str]] = []
        leg_status: dict[str, Any] = {}
        selected_id = str((selected or {}).get("intent", {}).get("intent_id") or "")
        if selected is not None:
            # Do not pass a pre-read leg status: an exact read-only check may
            # resolve a different planning stack. Use the status that was read
            # *after* that resolution, so the card never reports a stale leg.
            found, exact = self._submission_blockers(
                selected, now=self.clock(), live_view=facts, leg_status=None,
                stage="view",
            )
            blockers.extend(found)
            fresh_status = exact.get("leg_status") if isinstance(exact, Mapping) else None
            leg_status = dict(fresh_status) if isinstance(fresh_status, Mapping) else read_leg_status()
            if not exact:
                # The proposal was terminal, in doubt, or not armed, so the
                # live/leg blockers were not evaluated by the submission path.
                blockers.extend(self._live_blockers(
                    intent_id=selected_id, live_view=facts, leg_status=leg_status,
                ))
                if leg_status.get("error"):
                    blockers.append(_blocker("leg_status_unreadable", str(leg_status["error"])))
        else:
            leg_status = read_leg_status()
            blockers.extend(self._live_blockers(
                intent_id="", live_view=facts, leg_status=leg_status,
            ))
            if leg_status.get("error"):
                blockers.append(_blocker("leg_status_unreadable", str(leg_status["error"])))
            blockers.append(_blocker("proposal_missing", "No exact typed proposal has been recorded for /live."))
        if facts.get("error") and not any(item["reason"] == "live_view_unreadable" for item in blockers):
            blockers.append(_blocker("live_view_unreadable", str(facts["error"])))
        positions = [dict(item) for item in facts.get("positions") or ()
                     if isinstance(item, Mapping)]
        account = facts.get("account") if isinstance(facts.get("account"), Mapping) else {}
        currency = str(account.get("currency") or
                       getattr(getattr(getattr(self.platform, "setup", None), "account", None),
                               "account_currency", "") or "")
        profits = [_decimal(item.get("profit")) for item in positions]
        pnl_available = bool(positions) and all(value is not None for value in profits)
        pnl = {
            "available": pnl_available,
            "currency": currency,
            "total": str(sum((value or Decimal(0) for value in profits), Decimal(0))) if pnl_available else "",
            "basis": "broker-reported open position profit only",
            "reason": "" if pnl_available else "No complete broker-reported profit is available; balance, equity and closed P&L are not read.",
        }
        try:
            unresolved = [str(item) for item in self.repository.unresolved_order_ids(
                final_states=FINAL_ORDER_STATES)]
        except Exception:
            unresolved = []
        return {
            "available": True,
            "schema": SCHEMA_V3,
            "one_shot_submission_available": not blockers,
            "submission_available": not blockers,
            "submission_reason": blockers[0]["reason"] if blockers else "",
            "session_running": bool(facts.get("session_running")),
            "continuous_session_sending": bool(facts.get("next_signal_sends_real_order")),
            "evidence_gate": "exact signed independent replay and policy",
            "approval_file": self.approval_path.name,
            "approval_phrase": APPROVAL_PHRASE,
            "review_phrase": REVIEW_PHRASE,
            "arm_phrase": ARM_CONFIRMATION_PHRASE,
            "proposals": [self._summary(item) for item in states[:20]],
            "selected": self._summary(selected) if selected else {},
            "account_fingerprint": str(
                facts.get("confirmed_account_fingerprint") or facts.get("account_fingerprint") or ""
            ),
            "mode": str(facts.get("mode") or ""),
            "live_effective": bool(facts.get("live_effective")),
            "connected": bool(facts.get("connected")),
            "armed": bool(facts.get("armed")),
            "next_signal_sends_real_order": bool(facts.get("next_signal_sends_real_order")),
            "leg": leg_status,
            "blockers": blockers,
            "unresolved_orders": unresolved,
            "positions": positions,
            "pnl": pnl,
            "note": ("PAPER results cannot enter this bridge. No genuine AX-SCALPER candidate has passed "
                     "independent replay in this deployment by default; LIVE submission stays unavailable "
                     "until exact signed evidence and a separate exact-plan approval both exist. "
                     "Stopping, restarting or disconnecting does not close or reassign positions."),
        }


__all__ = [
    "APPROVAL_PHRASE", "EvidenceVerdict", "KIND", "REVIEW_PHRASE",
    "ScalperLiveIntentBridge", "blocked_evidence_gate", "paper_host_evidence_gate",
    "scalper_host_policy",
]
