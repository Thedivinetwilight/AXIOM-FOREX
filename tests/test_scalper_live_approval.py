"""Signed exact-plan LIVE approval; all keys and documents are synthetic."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from axiom_forex.scalper.live_approval import (
    APPROVAL_PHRASE,
    APPROVAL_SCHEMA,
    approval_identity,
    canonical_approval_payload,
    plan_fingerprint,
    verify_approval,
)
from tests.test_scalper_admission import NOW, policy_payload, setup_policy

UTC = timezone.utc
REVIEWED = NOW - timedelta(hours=1)
EXPIRES = NOW + timedelta(days=1)

INTENT = {
    "intent_id": "intent-exact", "candidate_fingerprint": "candidate-fp",
    "evidence_fingerprint": "evidence-digest", "account_fingerprint": "account-fp",
    "instrument_fingerprint": "instrument-fp", "symbol": "EUR/USD",
    "broker_symbol": "EURUSD.pro", "timeframe": "M1", "side": "BUY",
    "quantity": "0.10", "quantity_unit": "lots", "stop_price": "1.0990",
    "target_price": "1.1020", "quote_bid": "1.1000", "quote_ask": "1.1002",
    "quote_timestamp": (NOW - timedelta(seconds=2)).isoformat(),
    "quote_received_at": (NOW - timedelta(seconds=1)).isoformat(),
    "quote_source": "MT5_READ_ONLY", "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
    "risk_fingerprint": "risk-fp", "config_fingerprint": "config-fp",
}
PLAN = {
    "candidate_id": "candidate-fp", "pair": "EUR/USD", "broker_symbol": "EURUSD.pro",
    "timeframe": "1m", "quantity": "0.10", "max_open_positions": 1,
    "max_daily_loss_account": "100", "protective_stop_pips": "10",
    "account_currency": "USD", "evidence_state": "MEASURED",
}
EVIDENCE_DIGEST = "a" * 64


def fingerprint() -> str:
    return plan_fingerprint(intent=INTENT, plan=PLAN, evidence_digest=EVIDENCE_DIGEST)


def approval_document(**overrides):
    document = {
        "schema": APPROVAL_SCHEMA,
        "plan_phrase": APPROVAL_PHRASE,
        "plan_fingerprint": fingerprint(),
        "reviewer_id": "independent-reviewer",
        "strategy_developers": ["strategy-author"],
        "reviewed_at": REVIEWED.isoformat(),
        "expires_at": EXPIRES.isoformat(),
        **approval_identity(INTENT, evidence_digest=EVIDENCE_DIGEST),
    }
    document.update(overrides)
    return document


def write_approval(tmp_path: Path, private_key: Ed25519PrivateKey, *, mutations=None) -> Path:
    document = approval_document()
    if mutations:
        mutations(document)
    document["signature"] = private_key.sign(canonical_approval_payload(document)).hex()
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def key_and_policy(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_payload(public)), encoding="utf-8")
    return private, setup_policy(tmp_path, private)


def verify(path, policy):
    return verify_approval(
        path, policy=policy, expected_fingerprint=fingerprint(),
        expected_identity=approval_identity(INTENT, evidence_digest=EVIDENCE_DIGEST),
        now=NOW,
    )


def test_exact_signed_approval_is_valid_and_stable(tmp_path, key_and_policy):
    private, policy = key_and_policy
    path = write_approval(tmp_path, private)
    result = verify(path, policy)
    assert result.valid, result.reason
    assert result.reviewer == "independent-reviewer"
    assert len(result.fingerprint) == 64
    assert verify(path, policy).fingerprint == result.fingerprint
    # Changing any trading semantic changes the fingerprint the signature names.
    other = plan_fingerprint(
        intent={**INTENT, "quantity": "0.11"}, plan=PLAN,
        evidence_digest=EVIDENCE_DIGEST,
    )
    assert other != fingerprint()


@pytest.mark.parametrize(("mutations", "reason"), [
    (lambda x: x.update(plan_fingerprint="wrong"), "approval_plan_fingerprint_mismatch"),
    (lambda x: x.update(plan_phrase="APPROVE SOMETHING ELSE"), "approval_phrase_mismatch"),
    (lambda x: x.update(quantity="0.11"), "approval_quantity_mismatch"),
    (lambda x: x.update(evidence_digest="b" * 64), "approval_evidence_digest_mismatch"),
    (lambda x: x.update(reviewer_id="unknown"), "approval_reviewer_untrusted"),
    (lambda x: x.update(strategy_developers=["independent-reviewer"]), "approval_reviewer_not_independent"),
    (lambda x: x.update(expires_at=(NOW - timedelta(seconds=1)).isoformat()), "approval_time_window_invalid"),
    (lambda x: x.update(password="must-not-be-here"), "approval_contains_credential_field"),
])
def test_invalid_or_self_signed_approval_fails_closed(tmp_path, key_and_policy, mutations, reason):
    private, policy = key_and_policy
    path = write_approval(tmp_path, private, mutations=mutations)
    result = verify(path, policy)
    assert not result.valid
    assert result.reason == reason


def test_tampering_after_signature_is_refused(tmp_path, key_and_policy):
    private, policy = key_and_policy
    path = write_approval(tmp_path, private)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["target_price"] = "9.9999"
    path.write_text(json.dumps(document), encoding="utf-8")
    result = verify(path, policy)
    assert not result.valid
    assert result.reason in {"approval_target_price_mismatch", "approval_signature_invalid"}


def test_missing_approval_has_no_implicit_grant(tmp_path, key_and_policy):
    _, policy = key_and_policy
    result = verify(tmp_path / "missing.json", policy)
    assert not result.valid
    assert result.reason == "approval_missing"
