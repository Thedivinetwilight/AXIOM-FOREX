import base64
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from axiom_forex.scalper.admission import (
    EVALUATION_METHOD, EVIDENCE_SCHEMA, AdmissionError, canonical_payload,
    evaluate_artifact, evidence_digest, load_policy,
)
from axiom_forex.scalper.signals import default_strategies

UTC = timezone.utc
NOW = datetime(2026, 1, 5, tzinfo=UTC)
INSTRUMENT = {"symbol": "EURUSD.pro", "point": "0.00001", "contract_size": "100000"}


def policy_payload(public_key):
    return {
        "schema": "axiom-scalper-admission-policy-v1", "currency": "USD",
        "min_total_trades": 30, "min_oos_trades": 20, "min_walk_forward_folds": 2,
        "minimum_net_expectancy": "1.2", "minimum_win_rate_lcb_95": "0.70",
        "maximum_pbo": "0.25", "minimum_deflated_sharpe": "0.95",
        "minimum_profit_factor": "1.10", "maximum_q_value": "0.10",
        "maximum_drawdown": "1", "maximum_tail_loss_cvar_95": "1",
        "cost_stress_multiple": "1.5", "maximum_parameter_sensitivity_fraction": "0.5",
        "maximum_absolute_correlation": "0.8", "maximum_overlapping_exposure_fraction": "0.2",
        "maximum_artifact_age_days": 90,
        "trusted_reviewers": {"independent-reviewer": base64.b64encode(public_key).decode()},
    }


def signed_artifact(tmp_path, strategy, private_key, *, mutations=None):
    raw = tmp_path / "broker.csv"
    raw.write_text("timestamp,bid,ask\nfixture-data\n", encoding="utf-8")
    fingerprint = strategy.fingerprint
    train = ["2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00",
             "2026-01-01T02:00:00+00:00", "2026-01-01T03:00:00+00:00",
             "2026-01-01T04:00:00+00:00"]
    validation = [f"2026-01-02T0{i}:00:00+00:00" for i in range(5)]
    test = [f"2026-01-03T{i:02}:00:00+00:00" for i in range(20)]
    rows = []
    for split, stamps in (("train", train), ("validation", validation), ("test", test)):
        for index, stamp in enumerate(stamps):
            gross, net = (("-0.2", "-0.3") if split == "test" and index == 19 else
                          ("1.8", "1.7") if split == "test" else ("1.6", "1.5"))
            rows.append({"split": split, "timestamp": stamp, "gross_account": gross,
                         "cost_account": "0.1", "net_account": net, "regime": "TRENDING_UP"})
    peer = [float(i % 3) for i in range(20)]
    artifact = {
        "schema": EVIDENCE_SCHEMA,
        "reviewer_id": "independent-reviewer", "strategy_developers": ["strategy-author"],
        "strategy": {"strategy_id": strategy.name, "rule_version": strategy.rule_version,
                     "fingerprint": fingerprint},
        "instrument": INSTRUMENT,
        "horizon": {"timeframe": "M1", "holding_rule": "fixed stop or target; max 15 minutes"},
        "evaluation_method": EVALUATION_METHOD,
        "data": {"origin": "MT5_BROKER_HISTORICAL_EXPORT", "source": "local broker export",
                 "start": "2026-01-01T00:00:00+00:00", "end": "2026-01-03T23:00:00+00:00",
                 "file": raw.name, "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()},
        "costs": {"currency": "USD", "verified": True, "commission_per_lot": "7",
                  "commission_state": "OBSERVED", "spread_points": "10", "slippage_points": "1",
                  "slippage_state": "OBSERVED"},
        "trades": rows,
        "multiple_testing": {"pbo": "0.10", "deflated_sharpe": "0.98", "q_value": "0.05"},
        "replay": {"replay_version": "fixture-v1", "engine_digest": "fixture-engine",
                   "input_digest": "fixture-input", "output_digest": "fixture-output"},
        "walk_forward": [
            {"train_end": "2026-01-01T23:00:00+00:00", "test_start": "2026-01-02T00:00:00+00:00",
             "test_end": "2026-01-02T01:00:00+00:00", "test_net_account": ["1.4", "1.5"]},
            {"train_end": "2026-01-02T23:00:00+00:00", "test_start": "2026-01-03T00:00:00+00:00",
             "test_end": "2026-01-03T01:00:00+00:00", "test_net_account": ["1.3", "1.6"]},
        ],
        "parameter_sensitivity": [{"fingerprint": fingerprint, "mean_net_account": "1.6"},
                                  {"fingerprint": "nearby-variant", "mean_net_account": "1.5"}],
        "portfolio": {"returns": {fingerprint: [float(i) for i in range(20)],
                                    "peer-symbol": peer},
                      "overlap_intervals": [{"start": "2026-01-03T00:00:00+00:00",
                                             "end": "2026-01-03T01:00:00+00:00",
                                             "exposure_fraction": "0.1"}]},
    }
    if mutations:
        mutations(artifact)
    artifact["signature"] = private_key.sign(canonical_payload(artifact)).hex()
    target = tmp_path / f"{fingerprint}.json"
    target.write_text(json.dumps(artifact), encoding="utf-8")
    return target


def setup_policy(tmp_path, private_key):
    public = private_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                   serialization.PublicFormat.Raw)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_payload(public)), encoding="utf-8")
    return load_policy(path, expected_currency="USD")


def test_valid_signed_broker_evidence_passes_declared_policy(tmp_path):
    key = Ed25519PrivateKey.generate()
    strategy = default_strategies()[0]
    policy = setup_policy(tmp_path, key)
    artifact = signed_artifact(tmp_path, strategy, key)
    result = evaluate_artifact(artifact, policy=policy, strategy=strategy.spec(),
                               instrument=INSTRUMENT,
                               expected_costs={"currency": "USD", "commission_per_lot": "7",
                                               "commission_state": "OBSERVED", "slippage_points": "1",
                                               "slippage_state": "OBSERVED"},
                               replay_verifier=lambda _path, doc: {"verified": True, **doc["replay"]}, now=NOW)
    assert result.eligible, result.reason
    assert result.metrics["oos_trades"] == 20
    assert Decimal(result.metrics["net_expectancy_ci_lower_95"]) > Decimal("1.2")
    assert result.metrics["regime_outcomes"]["TRENDING_UP"] == {"wins": 19, "losses": 1}


@pytest.mark.parametrize(("mutations", "reason"), [
    (lambda x: x.update(instrument={"symbol": "wrong"}), "evidence_instrument_identity_mismatch"),
    (lambda x: x["strategy"].update(rule_version="wrong"), "evidence_strategy_identity_mismatch"),
    (lambda x: x["costs"].update(verified=False), "evidence_costs_unverified_or_currency_mismatch"),
    (lambda x: x["costs"].update(commission_per_lot="0"), "evidence_configured_cost_mismatch"),
    (lambda x: x["data"].update(origin="SYNTHETIC"), "evidence_data_origin_not_independent"),
    (lambda x: x["trades"][-1].update(net_account="99"), "evidence_net_cost_arithmetic_mismatch"),
    (lambda x: x["trades"][-1].update(timestamp="2026-01-02T03:00:00+00:00"), "evidence_chronology_invalid"),
    (lambda x: x["portfolio"]["overlap_intervals"][0].update(exposure_fraction="0.9"), "overlapping_exposure_above_policy"),
])
def test_signed_but_invalid_evidence_fails_closed(tmp_path, mutations, reason):
    key = Ed25519PrivateKey.generate()
    strategy = default_strategies()[0]
    policy = setup_policy(tmp_path, key)
    artifact = signed_artifact(tmp_path, strategy, key, mutations=mutations)
    result = evaluate_artifact(artifact, policy=policy, strategy=strategy.spec(),
                               instrument=INSTRUMENT,
                               expected_costs={"currency": "USD", "commission_per_lot": "7",
                                               "commission_state": "OBSERVED", "slippage_points": "1",
                                               "slippage_state": "OBSERVED"}, now=NOW)
    assert not result.eligible
    assert reason in result.reason


def test_high_win_rate_does_not_pass_negative_net_expectancy(tmp_path):
    key = Ed25519PrivateKey.generate()
    strategy = default_strategies()[0]
    policy = setup_policy(tmp_path, key)
    def high_win_but_loss(artifact):
        test_rows = [row for row in artifact["trades"] if row["split"] == "test"]
        for index, row in enumerate(test_rows):
            if index < 16:
                row.update(gross_account="0.2", cost_account="0.1", net_account="0.1")
            else:
                row.update(gross_account="-1.0", cost_account="0.1", net_account="-1.1")
        artifact["parameter_sensitivity"][0]["mean_net_account"] = "-0.14"
    artifact = signed_artifact(tmp_path, strategy, key, mutations=high_win_but_loss)
    result = evaluate_artifact(artifact, policy=policy, strategy=strategy.spec(),
                               instrument=INSTRUMENT,
                               expected_costs={"currency": "USD", "commission_per_lot": "7",
                                               "commission_state": "OBSERVED", "slippage_points": "1",
                                               "slippage_state": "OBSERVED"}, now=NOW)
    assert not result.eligible
    assert result.metrics["win_rate"] == 0.8
    assert Decimal(result.metrics["net_expectancy"]) < 0
    assert "net_expectancy_below_policy" in result.reason


def test_signature_and_reported_metrics_alone_cannot_open_admission(tmp_path):
    key = Ed25519PrivateKey.generate()
    strategy = default_strategies()[0]
    policy = setup_policy(tmp_path, key)
    artifact = signed_artifact(tmp_path, strategy, key)
    result = evaluate_artifact(artifact, policy=policy, strategy=strategy.spec(),
                               instrument=INSTRUMENT,
                               expected_costs={"currency": "USD", "commission_per_lot": "7",
                                               "commission_state": "OBSERVED", "slippage_points": "1",
                                               "slippage_state": "OBSERVED"}, now=NOW)
    assert not result.eligible
    assert result.reason == "evidence_raw_replay_verifier_unavailable"


def test_referenced_dataset_substitution_is_detected_by_digest(tmp_path):
    key = Ed25519PrivateKey.generate()
    strategy = default_strategies()[0]
    policy = setup_policy(tmp_path, key)
    artifact = signed_artifact(tmp_path, strategy, key)
    (tmp_path / "broker.csv").write_text("substituted dataset", encoding="utf-8")
    result = evaluate_artifact(artifact, policy=policy, strategy=strategy.spec(),
                               instrument=INSTRUMENT,
                               expected_costs={"currency": "USD", "commission_per_lot": "7",
                                               "commission_state": "OBSERVED", "slippage_points": "1",
                                               "slippage_state": "OBSERVED"}, now=NOW)
    assert result.reason == "evidence_data_digest_mismatch"


def test_replay_verifier_must_match_signed_input_and_output_digests(tmp_path):
    key = Ed25519PrivateKey.generate()
    strategy = default_strategies()[0]
    policy = setup_policy(tmp_path, key)
    artifact = signed_artifact(tmp_path, strategy, key)
    result = evaluate_artifact(artifact, policy=policy, strategy=strategy.spec(),
                               instrument=INSTRUMENT,
                               expected_costs={"currency": "USD", "commission_per_lot": "7",
                                               "commission_state": "OBSERVED", "slippage_points": "1",
                                               "slippage_state": "OBSERVED"},
                               replay_verifier=lambda _path, doc: {
                                   "verified": True, **doc["replay"], "output_digest": "wrong"
                               }, now=NOW)
    assert not result.eligible
    assert result.reason == "evidence_raw_replay_digest_mismatch"


def test_tampering_after_signature_is_rejected(tmp_path):
    key = Ed25519PrivateKey.generate()
    strategy = default_strategies()[0]
    policy = setup_policy(tmp_path, key)
    artifact = signed_artifact(tmp_path, strategy, key)
    document = json.loads(artifact.read_text())
    document["costs"]["commission_per_lot"] = "0"
    artifact.write_text(json.dumps(document))
    result = evaluate_artifact(artifact, policy=policy, strategy=strategy.spec(),
                               instrument=INSTRUMENT,
                               expected_costs={"currency": "USD", "commission_per_lot": "7",
                                               "commission_state": "OBSERVED", "slippage_points": "1",
                                               "slippage_state": "OBSERVED"}, now=NOW)
    assert result.reason == "evidence_signature_invalid"


def test_missing_and_malformed_policy_are_named_no_trade(tmp_path):
    with pytest.raises(AdmissionError, match="admission_policy_missing"):
        load_policy(tmp_path / "missing.json", expected_currency="USD")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"schema": "axiom-scalper-admission-policy-v1", "currency": "USD"}))
    with pytest.raises(AdmissionError, match="admission_policy_fields_missing"):
        load_policy(path, expected_currency="USD")
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    mismatch = policy_payload(public)
    mismatch["currency"] = "EUR"
    path.write_text(json.dumps(mismatch))
    with pytest.raises(AdmissionError, match="admission_policy_currency_mismatch"):
        load_policy(path, expected_currency="INR")


def test_verified_evidence_digest_binds_the_signed_artifact_and_exact_policy(tmp_path):
    key = Ed25519PrivateKey.generate()
    strategy = default_strategies()[0]
    policy = setup_policy(tmp_path, key)
    artifact = signed_artifact(tmp_path, strategy, key)
    result = evaluate_artifact(
        artifact, policy=policy, strategy=strategy.spec(), instrument=INSTRUMENT,
        expected_costs={"currency": "USD", "commission_per_lot": "7",
                        "commission_state": "OBSERVED", "slippage_points": "1",
                        "slippage_state": "OBSERVED"},
        replay_verifier=lambda _path, doc: {"verified": True, **doc["replay"]}, now=NOW,
    )
    assert result.eligible, result.reason
    assert len(result.evidence_digest) == 64
    document = json.loads(artifact.read_text(encoding="utf-8"))
    assert evidence_digest(document, policy) == result.evidence_digest
    changed_policy = replace(policy, min_total_trades=policy.min_total_trades + 1)
    assert evidence_digest(document, changed_policy) != result.evidence_digest
