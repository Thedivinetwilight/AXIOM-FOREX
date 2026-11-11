from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from axiom_forex.scalper.live_intent import IntentError, SCHEMA, SCHEMA_V2, SCHEMA_V3, ScalperLiveIntent

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
def payload():
    return {"schema":SCHEMA,"intent_id":"i-1","candidate_fingerprint":"candidate","evidence_fingerprint":"evidence","account_fingerprint":"account","instrument_fingerprint":"instrument","symbol":"EURUSD","timeframe":"M1","side":"BUY","quantity":"0.01","stop_price":"1.099","target_price":"1.102","quote_bid":"1.100","quote_ask":"1.101","quote_timestamp":(NOW-timedelta(seconds=1)).isoformat(),"quote_source":"MT5_READ_ONLY","expires_at":(NOW+timedelta(seconds=30)).isoformat(),"risk_fingerprint":"risk","config_fingerprint":"config"}
def test_version_identity_and_roundtrip():
    intent=ScalperLiveIntent.parse(payload(),now=NOW)
    assert intent.matches(candidate_fingerprint="candidate",evidence_fingerprint="evidence",account_fingerprint="account",instrument_fingerprint="instrument",symbol="EURUSD",timeframe="M1",risk_fingerprint="risk",config_fingerprint="config")
    assert not intent.matches(candidate_fingerprint="other",evidence_fingerprint="evidence",account_fingerprint="account",instrument_fingerprint="instrument",symbol="EURUSD",timeframe="M1",risk_fingerprint="risk",config_fingerprint="config")
    assert ScalperLiveIntent.parse(intent.as_payload(),now=NOW)==intent
    assert intent.quote_is_fresh(now=NOW,max_age_seconds=Decimal("2"))
    assert not intent.quote_is_fresh(now=NOW,max_age_seconds=Decimal("0.5"))
def test_malformed_expired_future_and_unsafe_quotes_refuse():
    with pytest.raises(IntentError,match="wrong_schema"): ScalperLiveIntent.parse({**payload(),"schema":"bad"},now=NOW)
    with pytest.raises(IntentError,match="expired"): ScalperLiveIntent.parse({**payload(),"expires_at":NOW.isoformat()},now=NOW)
    with pytest.raises(IntentError,match="future_quote"): ScalperLiveIntent.parse({**payload(),"quote_timestamp":(NOW+timedelta(seconds=1)).isoformat()},now=NOW)
    with pytest.raises(IntentError,match="invalid_quantity"): ScalperLiveIntent.parse({**payload(),"quantity":"0"},now=NOW)
    with pytest.raises(IntentError,match="invalid_buy_protections"): ScalperLiveIntent.parse({**payload(),"stop_price":"1.101"},now=NOW)
    with pytest.raises(IntentError,match="crossed_quote"): ScalperLiveIntent.parse({**payload(),"quote_bid":"1.102"},now=NOW)

def test_v2_preserves_the_declared_size_unit_and_all_identity_bindings():
    raw = {**payload(), "schema": SCHEMA_V2, "quantity_unit": "lots"}
    intent = ScalperLiveIntent.parse(raw, now=NOW)
    assert intent.quantity_unit == "lots"
    assert ScalperLiveIntent.parse(intent.as_payload(), now=NOW) == intent
    assert intent.as_payload()["quantity_unit"] == "lots"
    assert intent.matches(candidate_fingerprint="candidate", evidence_fingerprint="evidence",
                          account_fingerprint="account", instrument_fingerprint="instrument",
                          symbol="EURUSD", timeframe="M1", risk_fingerprint="risk",
                          config_fingerprint="config", quantity_unit="LOTS")
    assert not intent.matches(candidate_fingerprint="candidate", evidence_fingerprint="evidence",
                              account_fingerprint="account", instrument_fingerprint="instrument",
                              symbol="EURUSD", timeframe="M1", risk_fingerprint="risk",
                              config_fingerprint="config", quantity_unit="base_units")

def test_v2_refuses_a_missing_or_unknown_unit_without_guessing_it():
    raw = {**payload(), "schema": SCHEMA_V2}
    with pytest.raises(IntentError, match="missing_quantity_unit"):
        ScalperLiveIntent.parse(raw, now=NOW)
    with pytest.raises(IntentError, match="invalid_quantity_unit"):
        ScalperLiveIntent.parse({**raw, "quantity_unit": "contracts"}, now=NOW)

def test_v1_has_no_invented_unit_and_round_trips_unchanged():
    intent = ScalperLiveIntent.parse(payload(), now=NOW)
    assert intent.quantity_unit == ""
    assert intent.as_payload() == {**payload(), "quote_timestamp": (NOW-timedelta(seconds=1)).isoformat(),
                                   "expires_at": (NOW+timedelta(seconds=30)).isoformat()}


def v3_payload():
    return {**payload(), "schema": SCHEMA_V3, "quantity_unit": "lots",
            "broker_symbol": "EURUSD.pro",
            "quote_received_at": (NOW - timedelta(milliseconds=200)).isoformat()}


def test_v3_requires_and_preserves_exact_broker_and_quote_provenance():
    raw = v3_payload()
    intent = ScalperLiveIntent.parse(raw, now=NOW)
    assert intent.broker_symbol == "EURUSD.pro"
    assert intent.quote_received_at == NOW - timedelta(milliseconds=200)
    assert intent.feed_latency_seconds() == Decimal("0.8")
    assert ScalperLiveIntent.parse(intent.as_payload(), now=NOW) == intent
    assert intent.matches(candidate_fingerprint="candidate", evidence_fingerprint="evidence",
                          account_fingerprint="account", instrument_fingerprint="instrument",
                          symbol="EURUSD", timeframe="M1", risk_fingerprint="risk",
                          config_fingerprint="config", quantity_unit="LOTS",
                          broker_symbol="EURUSD.pro",
                          quote_received_at=NOW - timedelta(milliseconds=200))
    assert not intent.matches(candidate_fingerprint="candidate", evidence_fingerprint="evidence",
                              account_fingerprint="account", instrument_fingerprint="instrument",
                              symbol="EURUSD", timeframe="M1", risk_fingerprint="risk",
                              config_fingerprint="config", broker_symbol="EURUSD")


@pytest.mark.parametrize(("change", "reason"), [
    (lambda x: x.pop("broker_symbol"), "missing_broker_symbol"),
    (lambda x: x.pop("quote_received_at"), "missing_quote_received_at"),
    (lambda x: x.pop("quantity_unit"), "missing_quantity_unit"),
    (lambda x: x.update(quote_received_at=(NOW - timedelta(seconds=2)).isoformat()), "quote_received_before_timestamp"),
    (lambda x: x.update(quote_received_at=(NOW + timedelta(seconds=2)).isoformat()), "future_quote_received_at"),
    (lambda x: x.update(expires_at=(NOW - timedelta(seconds=1)).isoformat()), "expiry_not_after_quote"),
])
def test_v3_refuses_inferred_or_impossible_provenance(change, reason):
    raw = v3_payload()
    change(raw)
    with pytest.raises(IntentError, match=reason):
        ScalperLiveIntent.parse(raw, now=NOW)


def test_old_schemas_never_invent_v3_fields():
    for schema in (SCHEMA, SCHEMA_V2):
        raw = {**payload(), "schema": schema}
        if schema == SCHEMA_V2:
            raw["quantity_unit"] = "lots"
        intent = ScalperLiveIntent.parse(raw, now=NOW)
        assert intent.broker_symbol == ""
        assert intent.quote_received_at is None
        assert intent.feed_latency_seconds() is None
        assert "broker_symbol" not in intent.as_payload()
        assert "quote_received_at" not in intent.as_payload()
