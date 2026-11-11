"""Signed, reproducible evidence and explicit policy for scalper admission.

The module never fabricates data or selects thresholds. Missing policy fields,
unsigned/unverifiable artifacts, and any failed metric are ordinary NO-TRADE
results. Artifacts are Ed25519-signed by a locally trusted reviewer and bind the
review to the exact code, instrument, costs, data digest and evaluation payload.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

POLICY_SCHEMA = "axiom-scalper-admission-policy-v1"
EVIDENCE_SCHEMA = "axiom-scalper-evidence-v2"
EVALUATION_METHOD = "chronological_holdout_walk_forward_v1"
_POLICY_FIELDS = (
    "currency", "min_total_trades", "min_oos_trades", "min_walk_forward_folds",
    "minimum_net_expectancy", "minimum_win_rate_lcb_95", "maximum_pbo",
    "minimum_deflated_sharpe", "minimum_profit_factor", "maximum_q_value",
    "maximum_drawdown", "maximum_tail_loss_cvar_95", "cost_stress_multiple",
    "maximum_parameter_sensitivity_fraction", "maximum_absolute_correlation",
    "maximum_overlapping_exposure_fraction", "maximum_artifact_age_days",
    "trusted_reviewers",
)


class AdmissionError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AdmissionError("policy_value_invalid", field) from None
    if not result.is_finite():
        raise AdmissionError("policy_value_invalid", field)
    return result


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=str).encode("utf-8")


def evidence_digest(document: Mapping[str, Any], policy: AdmissionPolicy) -> str:
    """Content identity of signed evidence *and* the exact policy that judged it.

    The signature is excluded because it is the attestation, not the claim. The
    policy is included so a changed threshold cannot reuse the same identity.
    This is an identity, not an approval and not a profitability claim.
    """
    signed = {key: value for key, value in document.items() if key != "signature"}
    return hashlib.sha256(_canonical({"artifact": signed, "policy": policy.audit_view()})).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class AdmissionPolicy:
    currency: str
    min_total_trades: int
    min_oos_trades: int
    min_walk_forward_folds: int
    minimum_net_expectancy: Decimal
    minimum_win_rate_lcb_95: Decimal
    maximum_pbo: Decimal
    minimum_deflated_sharpe: Decimal
    minimum_profit_factor: Decimal
    maximum_q_value: Decimal
    maximum_drawdown: Decimal
    maximum_tail_loss_cvar_95: Decimal
    cost_stress_multiple: Decimal
    maximum_parameter_sensitivity_fraction: Decimal
    maximum_absolute_correlation: Decimal
    maximum_overlapping_exposure_fraction: Decimal
    maximum_artifact_age_days: int
    trusted_reviewers: Mapping[str, str]

    def audit_view(self) -> dict[str, Any]:
        return {key: (str(value) if isinstance(value, Decimal) else value)
                for key, value in self.__dict__.items()}


def load_policy(path: str | Path, *, expected_currency: str) -> AdmissionPolicy:
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AdmissionError("admission_policy_missing", f"Create {target} and set every required field; no threshold is assumed.") from None
    except (OSError, ValueError) as exc:
        raise AdmissionError("admission_policy_unreadable", f"{type(exc).__name__}: {target}") from None
    if not isinstance(raw, dict) or raw.get("schema") != POLICY_SCHEMA:
        raise AdmissionError("admission_policy_schema_invalid", f"Use schema {POLICY_SCHEMA}.")
    unknown = set(raw) - {"schema", *_POLICY_FIELDS}
    missing = [key for key in _POLICY_FIELDS if key not in raw or raw[key] in (None, "")]
    if unknown:
        raise AdmissionError("admission_policy_unknown_fields", ", ".join(sorted(unknown)))
    if missing:
        raise AdmissionError("admission_policy_fields_missing", ", ".join(missing))
    currency = str(raw["currency"]).strip().upper()
    if currency != expected_currency.upper():
        raise AdmissionError("admission_policy_currency_mismatch", f"Expected local paper currency {expected_currency.upper()}, policy declares {currency}.")
    integer_fields = ("min_total_trades", "min_oos_trades", "min_walk_forward_folds", "maximum_artifact_age_days")
    ints: dict[str, int] = {}
    for key in integer_fields:
        value = raw[key]
        if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
            raise AdmissionError("admission_policy_integer_invalid", key)
        ints[key] = int(value)
    decimals = {key: _decimal(raw[key], key) for key in _POLICY_FIELDS
                if key not in set(integer_fields) | {"currency", "trusted_reviewers"}}
    for key in ("minimum_net_expectancy", "maximum_drawdown", "maximum_tail_loss_cvar_95"):
        if decimals[key] <= 0:
            raise AdmissionError("admission_policy_range_invalid", key)
    for key in ("minimum_win_rate_lcb_95", "maximum_pbo", "minimum_deflated_sharpe",
                "maximum_q_value", "maximum_parameter_sensitivity_fraction",
                "maximum_absolute_correlation", "maximum_overlapping_exposure_fraction"):
        if not Decimal(0) <= decimals[key] <= Decimal(1):
            raise AdmissionError("admission_policy_range_invalid", key)
    if decimals["minimum_profit_factor"] <= 0:
        raise AdmissionError("admission_policy_range_invalid", "minimum_profit_factor")
    if decimals["cost_stress_multiple"] < 1:
        raise AdmissionError("admission_policy_range_invalid", "cost_stress_multiple must be at least 1x")
    reviewers = raw["trusted_reviewers"]
    if not isinstance(reviewers, dict) or not reviewers:
        raise AdmissionError("trusted_reviewers_missing", "Add reviewer_id -> base64 Ed25519 public key; public keys are not passwords.")
    for reviewer, encoded in reviewers.items():
        if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(encoded, str):
            raise AdmissionError("trusted_reviewer_invalid", "Reviewer IDs and encoded public keys must be strings.")
        try:
            if len(base64.b64decode(encoded, validate=True)) != 32:
                raise ValueError
        except Exception:
            raise AdmissionError("trusted_reviewer_key_invalid", reviewer) from None
    return AdmissionPolicy(currency=currency, **ints, **decimals, trusted_reviewers=dict(reviewers))


def _wilson_lower(wins: int, n: int, z: float = 1.959963984540054) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    den = 1 + z * z / n
    center = p + z * z / (2 * n)
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (center - radius) / den)


def _max_drawdown(returns: list[Decimal]) -> Decimal:
    equity = peak = Decimal(0)
    maximum = Decimal(0)
    for result in returns:
        equity += result
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _mean_ci_lower_95(returns: list[Decimal]) -> Decimal:
    """Deterministic percentile bootstrap lower bound; not a next-trade guarantee."""
    if len(returns) < 3:
        return Decimal("-Infinity")
    seed = int(hashlib.sha256(_canonical([str(x) for x in returns])).hexdigest()[:16], 16)
    rng = random.Random(seed)
    floats = [float(x) for x in returns]
    means = sorted(sum(floats[rng.randrange(len(floats))] for _ in floats) / len(floats)
                   for _ in range(1000))
    return Decimal(str(means[int(0.025 * (len(means) - 1))]))


def _cvar_95(returns: list[Decimal]) -> Decimal:
    if not returns:
        return Decimal(0)
    losses = sorted((max(Decimal(0), -x) for x in returns), reverse=True)
    count = max(1, math.ceil(len(losses) * 0.05))
    return sum(losses[:count], Decimal(0)) / count


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    mx, my = sum(left) / len(left), sum(right) / len(right)
    dx = [x - mx for x in left]
    dy = [y - my for y in right]
    xx, yy = sum(x*x for x in dx), sum(y*y for y in dy)
    if xx <= 0 or yy <= 0:
        return None
    return sum(x*y for x, y in zip(dx, dy)) / math.sqrt(xx * yy)


@dataclass(frozen=True)
class AdmissionResult:
    eligible: bool
    reason: str
    metrics: Mapping[str, Any]
    fingerprint: str = ""
    reviewer: str = ""
    artifact_path: str = ""
    evidence_digest: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reason": self.reason,
                "metrics": dict(self.metrics), "fingerprint": self.fingerprint,
                "reviewer": self.reviewer, "artifact_path": self.artifact_path,
                "evidence_digest": self.evidence_digest}


def evaluate_artifact(path: str | Path, *, policy: AdmissionPolicy,
                      strategy: Mapping[str, Any], instrument: Mapping[str, Any],
                      expected_costs: Mapping[str, Any],
                      replay_verifier: Callable[[Path, Mapping[str, Any]], Mapping[str, Any]] | None = None,
                      now: datetime | None = None) -> AdmissionResult:
    target = Path(path)
    digest = ""
    def refuse(reason: str, detail: str = "", metrics: Mapping[str, Any] | None = None) -> AdmissionResult:
        return AdmissionResult(False, reason if not detail else f"{reason}:{detail}", metrics or {},
                               str(strategy.get("fingerprint") or ""), artifact_path=str(target),
                               evidence_digest=digest)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return refuse("evidence_artifact_missing")
    except (OSError, ValueError) as exc:
        return refuse("evidence_artifact_unreadable", type(exc).__name__)
    if not isinstance(document, dict) or document.get("schema") != EVIDENCE_SCHEMA:
        return refuse("evidence_schema_invalid")
    signature = document.get("signature")
    reviewer = str(document.get("reviewer_id") or "")
    signed = {key: value for key, value in document.items() if key != "signature"}
    encoded_key = policy.trusted_reviewers.get(reviewer)
    if not encoded_key:
        return refuse("evidence_reviewer_untrusted", reviewer)
    if reviewer.lower() in {str(x).lower() for x in document.get("strategy_developers", [])}:
        return refuse("evidence_reviewer_not_independent", reviewer)
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded_key, validate=True))
        public.verify(bytes.fromhex(str(signature)), _canonical(signed))
    except ImportError:
        return refuse("evidence_signature_verifier_unavailable", "Install the evidence extra; no artifact is trusted without signature verification.")
    except (ValueError, TypeError, InvalidSignature):
        return refuse("evidence_signature_invalid")
    digest = evidence_digest(document, policy)
    fingerprint = str(strategy.get("fingerprint") or "")
    expected_identity = {"strategy_id": str(strategy.get("name") or ""),
                         "rule_version": str(strategy.get("rule_version") or ""),
                         "fingerprint": fingerprint}
    if document.get("strategy") != expected_identity:
        return refuse("evidence_strategy_identity_mismatch")
    if document.get("instrument") != dict(instrument):
        return refuse("evidence_instrument_identity_mismatch")
    horizon = document.get("horizon")
    if not isinstance(horizon, dict) or not horizon.get("timeframe") or not horizon.get("holding_rule"):
        return refuse("evidence_horizon_incomplete")
    if document.get("evaluation_method") != EVALUATION_METHOD:
        return refuse("evidence_evaluation_method_unsupported")
    data = document.get("data")
    if not isinstance(data, dict) or str(data.get("origin") or "").upper() not in {
        "MT5_BROKER_HISTORICAL_EXPORT", "MT5_BROKER_TICK_EXPORT", "MT5_BROKER_LIVE_CAPTURE"
    }:
        return refuse("evidence_data_origin_not_independent")
    if not all(data.get(key) for key in ("origin", "source", "start", "end", "file", "sha256")):
        return refuse("evidence_data_provenance_incomplete")
    try:
        root = target.resolve().parent
        source_candidate = root / str(data["file"])
        if source_candidate.is_symlink():
            return refuse("evidence_data_path_unsafe")
        source_file = source_candidate.resolve(strict=True)
        if not source_file.is_relative_to(root):
            return refuse("evidence_data_path_unsafe")
        if sha256_file(source_file) != str(data["sha256"]).lower():
            return refuse("evidence_data_digest_mismatch")
        start, end = datetime.fromisoformat(str(data["start"])), datetime.fromisoformat(str(data["end"]))
        current = now or datetime.now(timezone.utc)
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            return refuse("evidence_date_range_invalid")
        if end > current:
            return refuse("evidence_date_range_in_future")
        if (current - end).days > policy.maximum_artifact_age_days:
            return refuse("evidence_artifact_stale")
    except (OSError, ValueError, TypeError):
        return refuse("evidence_data_verification_failed")
    costs = document.get("costs")
    if not isinstance(costs, dict) or costs.get("currency") != policy.currency or costs.get("verified") is not True:
        return refuse("evidence_costs_unverified_or_currency_mismatch")
    if costs.get("commission_state") != "OBSERVED" or costs.get("slippage_state") != "OBSERVED":
        return refuse("evidence_costs_not_observed")
    for key in ("currency", "commission_per_lot", "commission_state", "slippage_points", "slippage_state"):
        actual = costs.get(key)
        expected = expected_costs.get(key)
        if key in {"commission_per_lot", "slippage_points"}:
            try:
                if _decimal(actual, key) != _decimal(expected, key):
                    return refuse("evidence_configured_cost_mismatch", key)
            except AdmissionError as exc:
                return refuse(exc.reason, exc.detail)
        elif actual != expected:
            return refuse("evidence_configured_cost_mismatch", key)
    for key in ("commission_per_lot", "spread_points", "slippage_points"):
        try:
            if _decimal(costs.get(key), key) < 0:
                return refuse("evidence_costs_invalid", key)
        except AdmissionError as exc:
            return refuse(exc.reason, exc.detail)
    rows = document.get("trades")
    folds = document.get("walk_forward")
    if not isinstance(rows, list) or not isinstance(folds, list):
        return refuse("evidence_results_missing")
    try:
        normalized: list[dict[str, Any]] = []
        phases = {"train": 0, "validation": 1, "test": 2}
        previous_phase, previous_time = -1, None
        for row in rows:
            if not isinstance(row, dict) or row.get("split") not in phases:
                return refuse("evidence_trade_schema_invalid")
            stamp = datetime.fromisoformat(str(row.get("timestamp") or ""))
            phase = phases[row["split"]]
            if stamp.tzinfo is None or stamp < start or stamp > end or (previous_time is not None and stamp < previous_time) or phase < previous_phase:
                return refuse("evidence_chronology_invalid")
            previous_phase, previous_time = phase, stamp
            gross, cost, net = (_decimal(row.get(k), k) for k in ("gross_account", "cost_account", "net_account"))
            if abs((gross - cost) - net) > Decimal("0.000001"):
                return refuse("evidence_net_cost_arithmetic_mismatch")
            if not row.get("regime"):
                return refuse("evidence_regime_missing")
            normalized.append({**row, "_net": net, "_gross": gross, "_cost": cost})
        train_end = max((datetime.fromisoformat(r["timestamp"]) for r in normalized if r["split"] == "train"), default=None)
        validation_start = min((datetime.fromisoformat(r["timestamp"]) for r in normalized if r["split"] == "validation"), default=None)
        validation_end = max((datetime.fromisoformat(r["timestamp"]) for r in normalized if r["split"] == "validation"), default=None)
        test_start = min((datetime.fromisoformat(r["timestamp"]) for r in normalized if r["split"] == "test"), default=None)
        if not all((train_end, validation_start, validation_end, test_start)) or train_end >= validation_start or validation_end >= test_start:
            return refuse("evidence_holdout_split_invalid")
        test = [r for r in normalized if r["split"] == "test"]
        if len(normalized) < policy.min_total_trades or len(test) < policy.min_oos_trades:
            return refuse("evidence_sample_insufficient", f"total={len(normalized)} test={len(test)}")
        net_returns = [r["_net"] for r in test]
        mean_net = sum(net_returns, Decimal(0)) / len(test)
        net_ci_lower = _mean_ci_lower_95(net_returns)
        wins = sum(x > 0 for x in net_returns)
        win_lcb = Decimal(str(_wilson_lower(wins, len(test))))
        multiple_testing = document.get("multiple_testing")
        if not isinstance(multiple_testing, dict):
            return refuse("evidence_multiple_testing_metrics_missing")
        pbo = _decimal(multiple_testing.get("pbo"), "pbo")
        deflated_sharpe = _decimal(multiple_testing.get("deflated_sharpe"), "deflated_sharpe")
        q_value = _decimal(multiple_testing.get("q_value"), "q_value")
        if not Decimal(0) <= pbo <= Decimal(1) or not Decimal(0) <= deflated_sharpe <= Decimal(1) or not Decimal(0) <= q_value <= Decimal(1):
            return refuse("evidence_multiple_testing_metric_invalid")
        gross_wins = sum((x for x in net_returns if x > 0), Decimal(0))
        gross_losses = abs(sum((x for x in net_returns if x <= 0), Decimal(0)))
        if gross_losses <= 0:
            return refuse("evidence_profit_factor_unavailable")
        profit_factor = gross_wins / gross_losses
        max_dd = _max_drawdown(net_returns)
        tail = _cvar_95(net_returns)
        stressed = sum((r["_gross"] - policy.cost_stress_multiple * r["_cost"] for r in test), Decimal(0)) / len(test)
        regimes: dict[str, list[Decimal]] = {}
        for row in test:
            regimes.setdefault(str(row["regime"]), []).append(row["_net"])
        regime_means = {name: sum(items, Decimal(0)) / len(items) for name, items in regimes.items()}
        if len(folds) < policy.min_walk_forward_folds:
            return refuse("evidence_walk_forward_folds_insufficient")
        fold_means: list[Decimal] = []
        previous_fold_test_end = None
        for fold in folds:
            if not isinstance(fold, dict):
                return refuse("evidence_walk_forward_fold_incomplete")
            try:
                train_end_fold = datetime.fromisoformat(str(fold.get("train_end") or ""))
                test_start_fold = datetime.fromisoformat(str(fold.get("test_start") or ""))
                test_end_fold = datetime.fromisoformat(str(fold.get("test_end") or ""))
            except ValueError:
                return refuse("evidence_walk_forward_fold_incomplete")
            if (train_end_fold.tzinfo is None or test_start_fold.tzinfo is None or test_end_fold.tzinfo is None
                    or train_end_fold < start or test_start_fold > end or test_end_fold > end
                    or train_end_fold >= test_start_fold or test_start_fold > test_end_fold
                    or (previous_fold_test_end is not None and test_start_fold <= previous_fold_test_end)):
                return refuse("evidence_walk_forward_chronology_invalid")
            previous_fold_test_end = test_end_fold
            values = [_decimal(v, "walk_forward_return") for v in fold.get("test_net_account", [])]
            if not values:
                return refuse("evidence_walk_forward_fold_incomplete")
            fold_means.append(sum(values, Decimal(0)) / len(values))
        variants = document.get("parameter_sensitivity")
        if not isinstance(variants, list) or len(variants) < 2 or any(not isinstance(x, dict) or not x.get("fingerprint") or x.get("mean_net_account") is None for x in variants):
            return refuse("evidence_parameter_sensitivity_missing")
        variant_means = [_decimal(x["mean_net_account"], "parameter_sensitivity") for x in variants]
        base_variant = next((x for x in variants if str(x["fingerprint"]) == fingerprint), None)
        if base_variant is None:
            return refuse("evidence_parameter_sensitivity_identity_missing")
        if abs(_decimal(base_variant["mean_net_account"], "parameter_sensitivity") - mean_net) > Decimal("0.000001"):
            return refuse("evidence_parameter_sensitivity_base_mismatch")
        sensitivity = (max(variant_means) - min(variant_means)) / max(abs(mean_net), Decimal("0.000001"))
        portfolio = document.get("portfolio")
        if not isinstance(portfolio, dict) or not isinstance(portfolio.get("returns"), dict) or not isinstance(portfolio.get("overlap_intervals"), list):
            return refuse("evidence_portfolio_analysis_missing")
        def finite_series(values: Any) -> list[float]:
            if not isinstance(values, list):
                raise ValueError("portfolio series must be lists")
            series = [float(x) for x in values]
            if any(not math.isfinite(x) for x in series):
                raise ValueError("portfolio series contains non-finite value")
            return series
        target_returns = finite_series(portfolio["returns"].get(fingerprint, []))
        correlations = []
        for peer, values in portfolio["returns"].items():
            if peer == fingerprint:
                continue
            corr = _correlation(target_returns, finite_series(values))
            if corr is None or not math.isfinite(corr):
                return refuse("evidence_portfolio_series_unverifiable", str(peer))
            correlations.append(abs(corr))
        if not correlations:
            return refuse("evidence_portfolio_comparison_missing")
        max_corr = max(correlations)
        intervals = portfolio["overlap_intervals"]
        if not intervals:
            return refuse("evidence_overlap_intervals_missing")
        events: list[tuple[datetime, int, Decimal]] = []
        for interval in intervals:
            if not isinstance(interval, dict) or not all(interval.get(k) for k in ("start", "end", "exposure_fraction")):
                return refuse("evidence_overlap_interval_invalid")
            a, b = datetime.fromisoformat(interval["start"]), datetime.fromisoformat(interval["end"])
            exposure = _decimal(interval["exposure_fraction"], "exposure_fraction")
            if a.tzinfo is None or b.tzinfo is None or a >= b or exposure < 0:
                return refuse("evidence_overlap_interval_invalid")
            events.extend(((a, 1, exposure), (b, -1, exposure)))
        active = peak = Decimal(0)
        for _stamp, delta, exposure in sorted(events, key=lambda e: (e[0], e[1])):
            active += exposure if delta > 0 else -exposure
            peak = max(peak, active)
        regime_outcomes = {name: {"wins": sum(x > 0 for x in items), "losses": sum(x <= 0 for x in items)}
                           for name, items in regimes.items()}
        metrics = {"total_trades": len(normalized), "oos_trades": len(test), "regime_outcomes": regime_outcomes,
                   "net_expectancy": str(mean_net), "net_expectancy_ci_lower_95": str(net_ci_lower), "win_rate": wins / len(test),
                   "win_rate_lcb_95": str(win_lcb), "pbo": str(pbo),
                   "deflated_sharpe": str(deflated_sharpe), "q_value": str(q_value),
                   "profit_factor": str(profit_factor), "max_drawdown": str(max_dd),
                   "tail_loss_cvar_95": str(tail), "cost_stress_expectancy": str(stressed),
                   "walk_forward_folds": len(folds), "walk_forward_means": [str(x) for x in fold_means],
                   "regime_means": {k: str(v) for k, v in regime_means.items()},
                   "parameter_sensitivity_fraction": str(sensitivity),
                   "maximum_absolute_correlation": max_corr,
                   "maximum_overlapping_exposure_fraction": str(peak)}
        fails = []
        if mean_net < policy.minimum_net_expectancy: fails.append("net_expectancy_below_policy")
        if net_ci_lower < policy.minimum_net_expectancy: fails.append("net_expectancy_uncertainty_below_policy")
        if win_lcb < policy.minimum_win_rate_lcb_95: fails.append("win_rate_confidence_below_policy")
        if pbo > policy.maximum_pbo: fails.append("pbo_above_policy")
        if deflated_sharpe < policy.minimum_deflated_sharpe: fails.append("deflated_sharpe_below_policy")
        if profit_factor < policy.minimum_profit_factor: fails.append("profit_factor_below_policy")
        if q_value > policy.maximum_q_value: fails.append("multiple_testing_q_value_above_policy")
        if max_dd > policy.maximum_drawdown: fails.append("drawdown_above_policy")
        if tail > policy.maximum_tail_loss_cvar_95: fails.append("tail_loss_above_policy")
        if stressed < policy.minimum_net_expectancy: fails.append("cost_stress_fails")
        if any(x < policy.minimum_net_expectancy for x in fold_means): fails.append("walk_forward_fold_fails")
        if any(len(values) < policy.min_oos_trades or sum(values, Decimal(0)) / len(values) < policy.minimum_net_expectancy for values in regimes.values()): fails.append("regime_stability_fails")
        if sensitivity > policy.maximum_parameter_sensitivity_fraction: fails.append("parameter_sensitivity_above_policy")
        if max_corr > float(policy.maximum_absolute_correlation): fails.append("portfolio_correlation_above_policy")
        if peak > policy.maximum_overlapping_exposure_fraction: fails.append("overlapping_exposure_above_policy")
        if fails:
            return AdmissionResult(False, ",".join(fails), metrics, fingerprint, reviewer, str(target),
                                   evidence_digest=digest)
        replay_manifest = document.get("replay")
        if not isinstance(replay_manifest, dict) or not all(
                replay_manifest.get(key) for key in ("replay_version", "engine_digest", "input_digest", "output_digest")):
            return refuse("evidence_replay_manifest_missing", metrics=metrics)
        if replay_verifier is None:
            return refuse("evidence_raw_replay_verifier_unavailable", metrics=metrics)
        try:
            reproduced = replay_verifier(target, document)
        except Exception as exc:
            return refuse("evidence_raw_replay_failed", type(exc).__name__, metrics)
        if not isinstance(reproduced, Mapping) or reproduced.get("verified") is not True:
            reason = str(reproduced.get("reason") or "evidence_raw_replay_mismatch") if isinstance(reproduced, Mapping) else "evidence_raw_replay_mismatch"
            return refuse(reason, metrics=metrics)
        if any(str(reproduced.get(key) or "") != str(replay_manifest.get(key) or "")
               for key in ("replay_version", "engine_digest", "input_digest", "output_digest")):
            return refuse("evidence_raw_replay_digest_mismatch", metrics=metrics)
        if replay_manifest.get("replay_version") == "axiom_scalper_raw_replay_v1":
            replay_results = reproduced.get("results")
            replay_test = replay_results.get("test") if isinstance(replay_results, Mapping) else None
            actual_trades = replay_test.get("account_trades") if isinstance(replay_test, Mapping) else None
            claimed_test = [row for row in rows if row.get("split") == "test"]
            if not isinstance(actual_trades, list) or len(actual_trades) != len(claimed_test):
                return refuse("evidence_replayed_test_trade_count_mismatch", metrics=metrics)
            try:
                for actual, claimed in zip(actual_trades, claimed_test):
                    close_at = datetime.fromtimestamp(int(actual["closed_at_ms"]) / 1000, tz=timezone.utc)
                    timestamp = datetime.fromisoformat(str(claimed["timestamp"]))
                    gross = _decimal(actual.get("gross_account"), "gross_account")
                    net = _decimal(actual.get("net_account"), "net_account")
                    if (timestamp != close_at or str(claimed.get("regime")) != str(actual.get("regime"))
                            or abs(_decimal(claimed.get("gross_account"), "gross_account") - gross) > Decimal("0.000001")
                            or abs(_decimal(claimed.get("cost_account"), "cost_account") - (gross-net)) > Decimal("0.000001")
                            or abs(_decimal(claimed.get("net_account"), "net_account") - net) > Decimal("0.000001")):
                        return refuse("evidence_replayed_test_trade_mismatch", metrics=metrics)
            except (KeyError, TypeError, ValueError, AdmissionError, OverflowError):
                return refuse("evidence_replayed_test_trade_claim_invalid", metrics=metrics)
        return AdmissionResult(True, "eligible_under_signed_policy_and_reproduced_replay", metrics,
                               fingerprint, reviewer, str(target), evidence_digest=digest)
    except (AdmissionError, KeyError, TypeError, ValueError, OverflowError) as exc:
        return refuse("evidence_metrics_invalid", str(exc))


def canonical_payload(document: Mapping[str, Any]) -> bytes:
    """Bytes an independent reviewer signs; signature itself is excluded."""
    return _canonical({key: value for key, value in document.items() if key != "signature"})
