"""Workstation host for a configured, read-only MT5 quote PAPER pump."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from os import O_RDONLY, close as os_close, fdopen, fsync, open as os_open, replace
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PAPER
from .desk import DeskThresholds, ScalperDesk
from .execution import PaperVenue
from .live_config import RISK_KEYS, LiveScalperConfigError, load_live_paper_config
from .live_quotes import LivePaperPump, MT5LiveQuoteSource
from . import bayes
from .admission import AdmissionError, evaluate_artifact, load_policy
from .raw_replay import verify_raw_artifact
from .skeptic import Criteria
from .signals import default_strategies
from .scribe import EXECUTION_RESULT, KINDS, POSITION_EXIT, POSITION_OPENED, RAW_TICK, REJECTION, STATE, Scribe, read_jsonl, verify_jsonl


class ScalperPaperHost:
    """PAPER controller owned by the workstation; no LIVE execution capability."""

    def __init__(self, *, session: Any, config_path: str | Path,
                 paper_root: str | Path, ledger_dir: str | Path):
        self.session = session
        self.config_path = Path(config_path)
        self.admission_policy_path = self.config_path.parent / "scalper_admission.local.json"
        self.evidence_dir = self.config_path.parent / "scalper_evidence"
        self.paper_root = Path(paper_root)
        self.ledger_dir = Path(ledger_dir)
        self.pump: LivePaperPump | None = None
        self.desk: ScalperDesk | None = None
        self.ledger_path: Path | None = None
        self._lock = threading.RLock()
        self._ledger_lock = threading.RLock()
        self._last_result: dict[str, Any] = {}
        self._observation_only = False

    @staticmethod
    def _checkpoint_path(ledger: Path) -> Path:
        return ledger.with_suffix(ledger.suffix + ".head.json")

    @classmethod
    def _write_checkpoint(cls, ledger: Path, desk: ScalperDesk) -> None:
        entries = desk.scribe.entries()
        head = entries[-1].hash if entries else "0" * 64
        payload = {"schema": "axiom-scalper-head-v1", "run_id": desk.scribe.run_id,
                   "entries": len(entries), "head": head}
        target = cls._checkpoint_path(ledger)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
        tmp = Path(raw_tmp)
        try:
            with fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                fsync(handle.fileno())
            replace(tmp, target)
            try:
                directory_fd = os_open(target.parent, O_RDONLY)
                try:
                    fsync(directory_fd)
                finally:
                    os_close(directory_fd)
            except OSError:
                pass
        except Exception:
            # A failed checkpoint blocks the run; do not repair or truncate logs.
            raise

    @classmethod
    def _verify_ledger(cls, path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            checkpoint = json.loads(cls._checkpoint_path(path).read_text(encoding="utf-8"))
        except Exception:
            return {"ok": False, "reason": "ledger_checkpoint_missing_or_invalid"}, []
        if not isinstance(checkpoint, dict) or checkpoint.get("schema") != "axiom-scalper-head-v1":
            return {"ok": False, "reason": "ledger_checkpoint_schema_invalid"}, []
        try:
            rows = read_jsonl(path)
            if any(row.get("kind") not in KINDS or not isinstance(row.get("payload"), dict)
                   for row in rows):
                return {"ok": False, "reason": "ledger_event_schema_invalid"}, rows
            run_ids = {str(row.get("run_id") or "") for row in rows}
            if run_ids != {str(checkpoint.get("run_id") or "")}:
                return {"ok": False, "reason": "ledger_checkpoint_run_id_mismatch"}, rows
            result = verify_jsonl(path, expected_head=str(checkpoint.get("head") or ""))
            if result.get("ok") and int(checkpoint.get("entries", -1)) != result.get("entries"):
                return {"ok": False, "reason": "ledger_checkpoint_count_mismatch"}, rows
            return result, rows
        except Exception as exc:
            return {"ok": False, "reason": f"ledger_read_failed:{type(exc).__name__}"}, []

    def _unresolved_ledgers(self, *, exclude_current: bool = False) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if not self.ledger_dir.exists():
            return found
        for path in sorted(self.ledger_dir.glob("*.jsonl")):
            if path.name == "recovery-decisions.jsonl":
                continue
            if exclude_current and self.ledger_path is not None and path == self.ledger_path:
                continue
            try:
                with self._ledger_lock:
                    integrity, rows = self._verify_ledger(path)
                if not integrity.get("ok"):
                    found.append({"path": str(path), "reason": str(integrity.get("reason") or "ledger_integrity_failed")})
                    continue
                opened: dict[str, dict[str, Any]] = {}
                opened_entry_ids: set[str] = set()
                closed: set[str] = set()
                uncertain: list[dict[str, Any]] = []
                filled_entry_ids: set[str] = set()
                for row in rows:
                    kind, payload = row.get("kind"), row.get("payload", {})
                    if str(payload.get("mode") or "").upper() != PAPER:
                        uncertain.append({"reason": "ledger_mode_not_paper_or_missing", "seq": row.get("seq")})
                    if kind == EXECUTION_RESULT:
                        request_id = str(payload.get("request_id") or "")
                        if payload.get("status") == "unknown_outcome":
                            uncertain.append({"request_id": request_id, "reason": "unknown_order_outcome"})
                        if payload.get("filled_lots") not in (None, "0", "0.0"):
                            if request_id:
                                filled_entry_ids.add(request_id)
                            else:
                                uncertain.append({"reason": "filled_execution_missing_request_id"})
                    elif kind == POSITION_OPENED:
                        pid = str(payload.get("position_id") or "")
                        request_id = str(payload.get("entry_request_id") or "")
                        if not pid:
                            uncertain.append({"reason": "position_missing_id", "seq": row.get("seq")})
                        elif pid in opened:
                            uncertain.append({"position_id": pid, "reason": "duplicate_position_open_event"})
                        else:
                            opened[pid] = {**payload, "position_event_seq": row.get("seq"),
                                           "position_event_hash": row.get("hash")}
                        if request_id:
                            if request_id in opened_entry_ids:
                                uncertain.append({"request_id": request_id,
                                                  "reason": "duplicate_position_entry_request_link"})
                            opened_entry_ids.add(request_id)
                        else:
                            uncertain.append({"position_id": pid, "reason": "position_missing_entry_request_id"})
                        required_state = (
                            "strategy_id", "strategy_version", "fingerprint", "symbol",
                            "instrument_identity", "side", "lots", "entry_price",
                            "stop_price", "entry_request_id", "risk_state",
                            "quote_tick_id", "quote_source", "quote_source_time",
                            "quote_received_at", "quote_bid", "quote_ask",
                        )
                        missing_state = [key for key in required_state if payload.get(key) in (None, "")]
                        if missing_state:
                            uncertain.append({"position_id": pid,
                                              "reason": "position_state_incomplete_for_exact_recovery",
                                              "missing_fields": missing_state})
                    elif kind == POSITION_EXIT:
                        pid = str(payload.get("position_id") or "")
                        if pid:
                            closed.add(pid)
                remaining = [dict(item, position_id=pid) for pid, item in opened.items() if pid not in closed]
                uncertain.extend({"request_id": request_id, "reason": "filled_execution_without_position_record"}
                                  for request_id in filled_entry_ids - opened_entry_ids)
                if remaining or uncertain:
                    found.append({"path": str(path),
                                  "reason": "open_or_unresolved_paper_state_requires_exact_recovery",
                                  "positions": remaining, "uncertain": uncertain})
                else:
                    manifest = self._replay_manifest(rows)
                    if manifest is not None:
                        found.append({"path": str(path),
                                      "reason": "exact_replay_verification_available",
                                      "positions": [], "uncertain": [],
                                      "replay_verification_available": True,
                                      "detail": "This strategy-free observer has a versioned input manifest. Start will replay every stored quote and compare every event before it resumes."})
                    else:
                        # Summary-only or strategy-bearing ledgers lack a proven
                        # deterministic restore path and remain untouched.
                        found.append({"path": str(path),
                                      "reason": "exact_scalper_state_restore_unavailable",
                                      "positions": [], "uncertain": [],
                                      "detail": "The ledger is intact and flat, but exact desk/guardian/tick state restoration is unavailable for this run; no replacement run may silently reset it."})
            except Exception as exc:
                found.append({"path": str(path), "reason": f"ledger_read_failed:{type(exc).__name__}"})
        return found

    def _persist_run(self, desk: ScalperDesk, ledger_path: Path) -> None:
        with self._ledger_lock:
            desk.scribe.to_jsonl(ledger_path)
            self._write_checkpoint(ledger_path, desk)

    @staticmethod
    def _config_fingerprint(config: Any, capital: Any, currency: str) -> str:
        payload = {"config": asdict(config), "capital": str(capital), "currency": currency}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str,
                                         separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def _replay_manifest(cls, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        starts = [row for row in rows if row.get("kind") == STATE
                  and row.get("payload", {}).get("state") == "live_quote_paper_started"]
        if len(starts) != 1:
            return None
        manifest = starts[0].get("payload", {}).get("recovery_manifest")
        if not isinstance(manifest, dict) or manifest.get("schema") != "axiom-scalper-replay-v1":
            return None
        if manifest.get("observation_only") is not True or manifest.get("strategies") != []:
            return None
        if not isinstance(manifest.get("config_fingerprint"), str):
            return None
        return manifest

    @classmethod
    def _replay_observation_ledger(
        cls, rows: list[dict[str, Any]], *, config: Any, capital: Any, currency: str,
    ) -> ScalperDesk:
        """Rebuild a strategy-free PAPER observer from its verified quote inputs.

        Legacy ledgers, strategy-bearing runs, unsupported event kinds and any
        replay mismatch remain blocked. This is deliberately a narrow recovery
        contract; it never edits the original JSONL or its checkpoint.
        """
        manifest = cls._replay_manifest(rows)
        if manifest is None:
            raise ValueError("exact_replay_manifest_unavailable")
        if manifest["config_fingerprint"] != cls._config_fingerprint(config, capital, currency):
            raise ValueError("paper_configuration_changed_since_run")
        run_id = str(rows[0].get("run_id") or "") if rows else ""
        if not run_id:
            raise ValueError("paper_run_id_missing")
        desk = ScalperDesk(config=config, strategies=(), venue=PaperVenue(cost=config.cost),
                           starting_equity=capital, run_id=run_id)
        external_states = {"live_quote_paper_started", "paper_resumed_exactly",
                           "paper_stopped_positions_left_open"}
        for row in rows:
            kind, payload = row.get("kind"), dict(row.get("payload") or {})
            at_ms, actor = int(row["at_ms"]), str(row.get("actor") or "desk")
            payload.pop("mode", None)
            if kind == RAW_TICK:
                required = ("symbol", "bid", "ask", "source_time", "received_at", "source")
                if any(payload.get(key) in (None, "") for key in required):
                    raise ValueError("raw_tick_input_incomplete")
                desk.scribe.record(RAW_TICK, at_ms=at_ms, actor=actor, **payload)
                desk.on_tick(symbol=str(payload["symbol"]), bid=payload["bid"], ask=payload["ask"],
                             source_time=datetime.fromisoformat(str(payload["source_time"])),
                             received_at=datetime.fromisoformat(str(payload["received_at"])),
                             source=str(payload["source"]), volume=payload.get("volume"))
            elif kind == STATE and payload.get("state") in external_states:
                desk.scribe.record(STATE, at_ms=at_ms, actor=actor, **payload)
            elif kind == REJECTION and payload.get("stage") == "live_quote_source":
                reason = str(payload.pop("reason", ""))
                stage = str(payload.pop("stage", ""))
                desk.scribe.reject(at_ms=at_ms, actor=actor, reason=reason, stage=stage, **payload)
            elif kind in {STATE, REJECTION, "system_error"}:
                raise ValueError("unsupported_external_event_in_paper_ledger")
        expected = [row for row in rows]
        actual = [entry.as_dict() for entry in desk.scribe.entries()]
        if len(actual) != len(expected):
            raise ValueError("paper_replay_event_count_mismatch")
        for index, (left, right) in enumerate(zip(actual, expected), start=1):
            if left != right:
                raise ValueError(f"paper_replay_mismatch_at_event_{index}")
        if desk.book.open_positions or desk.status().get("unresolved"):
            raise ValueError("observation_replay_unexpected_position_or_order")
        desk.scribe.mark_persisted_prefix(len(rows))
        return desk

    def _strategy_review(self, *, currency: str = "", instrument: dict[str, Any] | None = None,
                         expected_costs: dict[str, Any] | None = None, config: Any | None = None) -> dict[str, Any]:
        criteria = Criteria()
        policy = None
        policy_error = ""
        try:
            if not currency:
                raise AdmissionError("paper_currency_unavailable", "Connect and configure local PAPER capital.")
            policy = load_policy(self.admission_policy_path, expected_currency=currency)
        except AdmissionError as exc:
            policy_error = f"{exc.reason}: {exc.detail}"
        candidates = []
        for strategy in default_strategies():
            result = None
            if policy is None:
                reason = policy_error
            elif instrument is None or expected_costs is None:
                reason = "verified instrument and exact observed cost settings are required before evidence can be matched"
            else:
                artifact_path = self.evidence_dir / f"{strategy.fingerprint}.json"
                verifier = None
                if config is not None:
                    verifier = lambda path, document, s=strategy: verify_raw_artifact(
                        path, document, config=config, strategy=s,
                        instrument=instrument or {}, expected_costs=expected_costs or {})
                result = evaluate_artifact(artifact_path, policy=policy,
                                           strategy=strategy.spec(), instrument=instrument,
                                           expected_costs=expected_costs,
                                           replay_verifier=verifier)
                reason = result.reason
            candidates.append({
                **strategy.spec(),
                "eligible": bool(result and result.eligible),
                "reason": "" if result and result.eligible else reason,
                "metrics": {} if result is None else dict(result.metrics),
                "evidence": {} if result is None else result.audit_view(),
                "artifact_path": str(self.evidence_dir / f"{strategy.fingerprint}.json"),
                "required_evidence": [
                    "trusted Ed25519 reviewer signature, distinct from strategy developers",
                    "axiom_scalper_raw_replay_v1 over signed broker CSV, train calibration, validation/test, and declared walk-forward PAPER replays",
                    "digest-verified broker historical/tick dataset and date range",
                    "chronological train/validation/test separation and walk-forward folds",
                    "net expectancy with bootstrap confidence, and a win-rate confidence bound",
                    "cost stress, parameter sensitivity, drawdown and 95% CVaR tail loss",
                    "regime stability, peer-return correlation and overlapping exposure",
                ],
            })
        count = sum(bool(item["eligible"]) for item in candidates)
        questions = []
        if policy is None:
            questions.append(policy_error or "Admission policy is not configured.")
        if not instrument:
            questions.append("Verified broker instrument/cost terms are missing; no evidence can be matched.")
        if not count:
            questions.append("No signed, exact-fingerprint evidence artifact currently passes every configured check.")
        questions.append("This host is one symbol at a time; portfolio-wide multi-symbol risk aggregation is not available.")
        return {
            "policy": criteria.audit_view(),
            "policy_path": str(self.admission_policy_path),
            "evidence_dir": str(self.evidence_dir),
            "policy_status": "configured" if policy else "blocked",
            "effective_policy": {} if policy is None else policy.audit_view(),
            "candidates": candidates,
            "eligible_count": count,
            "trade_admission_open": count > 0,
            "decision": (f"{count} exact strategy fingerprint(s) pass signed evidence and policy checks"
                         if count else "NO TRADE: policy or independent signed evidence is missing or failed validation"),
            "open_policy_questions": questions,
            "live_route": "No scalper intent routes from this host. An eligible PAPER result does not authorize LIVE; use only the separately confirmed and armed existing /live workflow.",
        }

    def status(self) -> dict[str, Any]:
        connected = bool(getattr(self.session, "connected", False))
        pump_state = str(self.pump.status().get("state")) if self.pump else ""
        running = pump_state == "RUNNING"
        recovery = self._unresolved_ledgers(exclude_current=pump_state in {"RUNNING", "PAUSED"})
        state = "DISCONNECTED" if not connected else "BLOCKED" if recovery else "STOPPED"
        reason = "terminal_not_connected" if not connected else "recovery_required" if recovery else ""
        quote_status = self.pump.status() if self.pump else {}
        if not connected:
            state, reason = "DISCONNECTED", "terminal_not_connected"
        elif recovery:
            state, reason = "BLOCKED", "recovery_required"
        elif quote_status.get("state"):
            state, reason = str(quote_status.get("state")), str(quote_status.get("reason") or "")
        if self.desk is not None:
            quote_status["strategy_versions"] = [
                {"strategy": item.name, "version": item.rule_version, "fingerprint": item.fingerprint}
                for item in self.desk.strategies
            ]
            if self.ledger_path is not None:
                with self._ledger_lock:
                    quote_status["ledger_integrity"] = self._verify_ledger(self.ledger_path)[0]
        account = self.session.account() if connected else {}
        try:
            from axiom_forex.workstation.symbols import build_catalogue
            catalogue = build_catalogue(tuple(self.session.symbols()), specs_reported=True)
            symbols = [item.audit_view() for item in catalogue.pairs]
            exclusions = [dict(item) for item in catalogue.excluded]
        except Exception:
            symbols, exclusions = [], [{"reason": "terminal_symbol_catalogue_unavailable"}]
        config_view: dict[str, Any] = {}
        instrument_view: dict[str, Any] | None = None
        expected_costs: dict[str, Any] | None = None
        try:
            if connected:
                config, capital, currency = load_live_paper_config(
                    session=self.session, settings_path=self.config_path, paper_root=self.paper_root)
                instrument_view = config.instruments[0].audit_view()
                expected_costs = {"currency": currency, **config.cost.audit_view()}
                config_view = {
                    "symbol": config.instruments[0].symbol,
                    "max_quote_age_ms": config.max_quote_age_ms,
                    "point": str(config.instruments[0].point),
                    "contract_size": str(config.instruments[0].contract_size),
                    "capital": str(capital), "currency": currency,
                    "commission_per_lot": str(config.cost.commission_per_lot),
                    "commission_state": config.cost.commission_state,
                    "strategy_admission": "NOT_APPROVED: probability source is absent; desk refuses entries",
                    "slippage_points_per_side": str(config.cost.slippage_points),
                    "slippage_state": config.cost.slippage_state,
                    "risk": {key: str(getattr(config, key)) for key in RISK_KEYS},
                }
            else:
                load_live_paper_config(session=self.session,
                    settings_path=self.config_path, paper_root=self.paper_root)
        except LiveScalperConfigError as exc:
            config_view = {"reason": exc.reason, "remediation": exc.remediation}
            if state not in {"DISCONNECTED", "BLOCKED"}:
                state, reason = "BLOCKED", exc.reason
        review = self._strategy_review(currency=str(account.get("currency") or ""),
                                       instrument=instrument_view, expected_costs=expected_costs,
                                       config=config if connected and "config" in locals() else None)
        observation_start_allowed = bool(connected and not recovery and not running and instrument_view
                                         and state == "STOPPED")
        resume_exact_allowed = bool(connected and not running and recovery
                                    and all(item.get("replay_verification_available") for item in recovery))
        trading_start_allowed = bool(observation_start_allowed and review.get("trade_admission_open"))
        quote_age = quote_status.get("quote_age_seconds")
        quote_fresh = bool(running and quote_age is not None
                           and int(float(quote_age) * 1000) <= int(config_view.get("max_quote_age_ms", 0))
                           and int((quote_status.get("accepted_ticks_by_symbol") or {}).get(
                               str(config_view.get("symbol") or ""), 0)) > 0)
        return {
            "available": True, "mode": PAPER, "source": "MT5_LIVE_PAPER",
            "state": state, "connected": connected, "reason": reason,
            "config": {"path": str(self.config_path), **config_view},
            "account_type": str(account.get("mode") or "UNKNOWN"),
            "symbols": symbols, "symbol_exclusions": exclusions,
            "recovery": recovery, "quote": quote_status, "paper_only": True,
            "recovery_capability": {
                "exact_restore_available": False,
                "replay_verification_available": bool(recovery and all(
                    item.get("replay_verification_available") for item in recovery)),
                "confirmed_action": "KEEP_PAPER_BOOK_BLOCKED",
                "action_effect": ("Start can verify and resume the same strategy-free observation ledger" if resume_exact_allowed
                                  else "review is recorded; unverified or legacy ledgers remain unchanged and blocked"),
            },
            "strategy_review": review,
            "observation_start_allowed": observation_start_allowed,
            "resume_exact_allowed": resume_exact_allowed,
            "start_allowed": trading_start_allowed,
            "quote_fresh": quote_fresh,
            "trading_ready": bool(review.get("trade_admission_open") and running and not recovery
                                  and quote_fresh),
            "observation_only": bool(self._observation_only and running),
            # PAPER signals never mint LIVE authority. Independent replay admission
            # and separate LIVE approval are prerequisites; execution remains solely
            # in the established /live workflow.
            "live_handoff": {"available": False, "status": "BLOCKED",
                             "reason": "independent_replay_admission_required",
                             "intent_schema": "axiom-scalper-live-intent-v1",
                             "route": "No scalper order route exists; admitted proposals must enter the existing /live workflow."},
            "last_result": dict(self._last_result),
            "ledger": str(self.ledger_path or ""),
        }

    def start(self, *, observation_only: bool = False) -> dict[str, Any]:
        """Start entries only for admitted evidence, or a distinct no-entry observer."""
        with self._lock:
            if self.pump and self.pump.status().get("state") == "RUNNING":
                return {"ok": False, "reason": "already_running", "message": "AX-SCALPER PAPER is already running."}
            if self.pump and self.pump.status().get("state") == "BLOCKED":
                return {"ok": False, "reason": "runner_blocked", "message": "The previous AX-SCALPER run is blocked. Review its ledger; no replacement runner was created."}
            if not self.session.connected:
                return self._refuse("terminal_not_connected", "Press Connect on Setup; AX-SCALPER never connects in the background.")
            recovery = self._unresolved_ledgers()
            replay_candidates = [item for item in recovery
                                 if item.get("replay_verification_available")]
            if recovery and (len(recovery) != 1 or len(replay_candidates) != 1):
                return self._refuse("recovery_required", "A prior PAPER ledger cannot be safely resumed. Its stored positions and ledger remain untouched and new runs are blocked.", recovery=recovery)
            try:
                config, capital, currency = load_live_paper_config(
                    session=self.session, settings_path=self.config_path, paper_root=self.paper_root)
            except LiveScalperConfigError as exc:
                return self._refuse(exc.reason, exc.remediation)
            if replay_candidates:
                ledger_path = Path(str(replay_candidates[0]["path"]))
                with self._ledger_lock:
                    integrity, rows = self._verify_ledger(ledger_path)
                if not integrity.get("ok"):
                    return self._refuse("recovery_required", "The PAPER ledger changed or failed integrity verification; it was left untouched.")
                try:
                    desk = self._replay_observation_ledger(
                        rows, config=config, capital=capital, currency=currency)
                except Exception as exc:
                    return self._refuse("exact_replay_refused",
                        f"Exact PAPER recovery could not be proven ({exc}). The ledger was left unchanged and Start remains blocked.")
                desk.scribe.record(STATE, at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                                   actor="workstation", state="paper_resumed_exactly",
                                   replayed_entries=len(rows), replay_head=integrity.get("head"))
                self._persist_run(desk, ledger_path)
                source = MT5LiveQuoteSource(self.session, max_quote_age_ms=config.max_quote_age_ms)
                pump = LivePaperPump(source=source, desk=desk,
                                     symbol=config.instruments[0].symbol,
                                     persist=lambda: self._persist_run(desk, ledger_path))
                outcome = pump.start()
                if not outcome.get("ok"):
                    return self._refuse(str(outcome.get("reason") or "runner_start_refused"),
                        "The exact ledger was restored, but the quote pump did not start. No broker order was sent.")
                self.desk, self.pump, self.ledger_path = desk, pump, ledger_path
                self._observation_only = True
                self._last_result = {"ok": True, "reason": "exact_recovery_resumed",
                    "message": "Every stored PAPER input and event replayed exactly. The same strategy-free observation run resumed; no order was sent."}
                return {**self.status(), **self._last_result}
            review = self._strategy_review(currency=currency, instrument=config.instruments[0].audit_view(),
                                           expected_costs={"currency": currency, **config.cost.audit_view()},
                                           config=config)
            eligible = {item["fingerprint"]: item for item in review["candidates"] if item["eligible"]}
            if not observation_only and not eligible:
                return self._refuse("no_admitted_strategy",
                    "No candidate passed the signed policy and independent raw replay. PAPER decision start is blocked; choose the separate observation-only action if you only need quotes.")
            strategies = (() if observation_only else
                          tuple(item for item in default_strategies() if item.fingerprint in eligible))
            admitted_outcomes = bayes.OutcomeBook()
            for fingerprint, candidate in eligible.items():
                for regime, counts in candidate["metrics"].get("regime_outcomes", {}).items():
                    for _ in range(int(counts.get("wins", 0))):
                        admitted_outcomes.record(fingerprint=fingerprint, regime=regime, won=True)
                    for _ in range(int(counts.get("losses", 0))):
                        admitted_outcomes.record(fingerprint=fingerprint, regime=regime, won=False)
            policy_min_samples = int(review["effective_policy"].get("min_oos_trades", 30))
            run_id = "scalper-paper-" + uuid.uuid4().hex
            self.ledger_dir.mkdir(parents=True, exist_ok=True)
            ledger_path = self.ledger_dir / f"{run_id}.jsonl"
            desk = ScalperDesk(config=config, strategies=strategies,
                               venue=PaperVenue(cost=config.cost),
                               outcome_book=bayes.OutcomeBook(),
                               probability_source=lambda signal, regime: admitted_outcomes.estimate(
                                   signal.parameter_fingerprint, regime),
                               thresholds=DeskThresholds(min_probability_samples=policy_min_samples),
                               starting_equity=capital, run_id=run_id)
            instrument = config.instruments[0]
            fingerprint = hashlib.sha256(json.dumps({
                "mode": PAPER,
                "symbol": instrument.symbol,
                "instrument": instrument.audit_view(),
                "capital": str(capital),
                "risk": {key: str(getattr(config, key)) for key in RISK_KEYS},
                "cost": config.cost.audit_view(),
                "strategies": [{"name": item.name, "version": item.rule_version,
                                "fingerprint": item.fingerprint}
                               for item in desk.strategies],
            }, sort_keys=True, default=str).encode()).hexdigest()
            desk.scribe.record(STATE, at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                               actor="workstation", state="live_quote_paper_started",
                               config_fingerprint=fingerprint, strategy_versions=[
                                   {"strategy": item.name, "version": item.rule_version, "fingerprint": item.fingerprint}
                                   for item in desk.strategies],
                               recovery_manifest={
                                   "schema": "axiom-scalper-replay-v1",
                                   "observation_only": bool(observation_only and not desk.strategies),
                                   "strategies": [item.fingerprint for item in desk.strategies],
                                   "config_fingerprint": self._config_fingerprint(config, capital, currency),
                               })
            self._persist_run(desk, ledger_path)
            source = MT5LiveQuoteSource(self.session, max_quote_age_ms=config.max_quote_age_ms)
            pump = LivePaperPump(source=source, desk=desk,
                                 symbol=config.instruments[0].symbol,
                                 persist=lambda: self._persist_run(desk, ledger_path))
            outcome = pump.start()
            if not outcome.get("ok"):
                return self._refuse(str(outcome.get("reason") or "runner_start_refused"),
                                    "No PAPER decisions were started; check the connected session and local settings.")
            self.desk, self.pump, self.ledger_path = desk, pump, ledger_path
            self._observation_only = bool(observation_only)
            self._last_result = {"ok": True, "reason": "", "message": (
                "AX-SCALPER PAPER quote observation started. "
                + ("Only independently replayed, policy-approved strategy fingerprints are enabled. " if strategies else
                   "Observation-only: no strategies are attached and no PAPER entries or trades can be generated. ")
                + "This is PAPER only; it does not imply profitability or authorize LIVE execution.")}
            return {**self._last_result, **self.status()}

    def select_symbol(self, *, symbol: str) -> dict[str, Any]:
        """Persist only an exact, currently verified tradable Forex symbol."""
        with self._lock:
            if self.pump and self.pump.status().get("state") == "RUNNING":
                return self._refuse("stop_before_symbol_change", "Stop AX-SCALPER PAPER before changing its selected broker symbol.")
            if not self.session.connected:
                return self._refuse("terminal_not_connected", "Press Connect on Setup; symbol selection never connects in the background.")
            from axiom_forex.workstation.symbols import build_catalogue
            try:
                catalogue = build_catalogue(tuple(self.session.symbols()), specs_reported=True)
            except Exception as exc:
                return self._refuse("terminal_symbol_catalogue_unavailable", f"Refresh the connected terminal catalogue ({type(exc).__name__}).")
            candidate = next((item for item in catalogue.pairs if item.broker_symbol == symbol), None)
            if candidate is None or not candidate.usable or not candidate.tradable:
                return self._refuse("scalper_symbol_not_verified", "Choose an exact, verified, tradable Forex symbol shown by this workstation.")
            try:
                if self.config_path.exists():
                    payload = json.loads(self.config_path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict) or payload.get("schema") != "axiom-scalper-paper-v1":
                        return self._refuse("scalper_settings_schema_invalid", "The existing local settings were left untouched; repair their schema before selecting a symbol.")
                else:
                    payload = {"schema": "axiom-scalper-paper-v1", "margin_rate": "",
                               "costs": {}, "risk": {}}
                payload["symbol"] = symbol
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                fd, raw_tmp = tempfile.mkstemp(prefix=self.config_path.name + ".", suffix=".tmp", dir=self.config_path.parent)
                try:
                    with fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle, indent=2, sort_keys=True)
                        handle.write("\n")
                        handle.flush()
                        fsync(handle.fileno())
                    replace(raw_tmp, self.config_path)
                except Exception:
                    try:
                        Path(raw_tmp).unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
            except Exception as exc:
                return self._refuse("scalper_symbol_save_failed", f"The local selection was not saved ({type(exc).__name__}); check file permissions and preserve the existing file.")
            self._last_result = {"ok": True, "reason": "", "message": f"Selected exact verified broker symbol {symbol}. Other settings were preserved; missing terms still block Start."}
            return dict(self._last_result)

    def acknowledge_recovery(self, *, confirmation: str) -> dict[str, Any]:
        """Record the operator's choice to keep an unresumable book blocked."""
        if confirmation != "KEEP_PAPER_BOOK_BLOCKED":
            return self._refuse("recovery_confirmation_mismatch", "Type KEEP_PAPER_BOOK_BLOCKED exactly. This records review only; it does not resume, flatten or clear the blocker.")
        recovery = self._unresolved_ledgers()
        if not recovery:
            return self._refuse("no_recovery_required", "There is no unresolved scalper paper ledger to acknowledge.")
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        path = self.ledger_dir / "recovery-decisions.jsonl"
        record = {"schema": "axiom-scalper-recovery-decision-v1",
                  "decision": "KEEP_PAPER_BOOK_BLOCKED",
                  "at": datetime.now(timezone.utc).isoformat(),
                  "ledgers": [item.get("path") for item in recovery],
                  "warning": "acknowledgement does not resume or close positions"}
        with self._ledger_lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            fsync(handle.fileno())
        self._last_result = {"ok": True, "reason": "recovery_remains_blocked",
                             "message": "Review recorded. Stored paper positions remain untouched and AX-SCALPER Start remains blocked until exact manual recovery is available."}
        return dict(self._last_result)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self.pump is None:
                self._last_result = {"ok": True, "reason": "already_stopped", "message": "AX-SCALPER PAPER is stopped; no positions were changed."}
                return dict(self._last_result)
            status = self.pump.stop("operator_stop")
            if self.desk is not None and self.ledger_path is not None:
                self.desk.scribe.record(STATE, at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                                        actor="workstation", state="paper_stopped_positions_left_open",
                                        open_positions=[item.audit_view() for item in self.desk.book.open_positions])
                self._persist_run(self.desk, self.ledger_path)
            self._observation_only = False
            self._last_result = {"ok": True, "reason": "", "message": "AX-SCALPER PAPER stopped. Local paper positions remain open and were not flattened."}
            return {**self._last_result, **status, "ledger": str(self.ledger_path or "")}

    def _refuse(self, reason: str, message: str, **extra: Any) -> dict[str, Any]:
        self._last_result = {"ok": False, "reason": reason, "message": message, **extra}
        return dict(self._last_result)
