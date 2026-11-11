from datetime import datetime, timedelta, timezone
import pytest
from axiom_forex.scalper.live_intent import IntentError, SCHEMA, ScalperLiveIntent

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
def payload():
    return {"schema":SCHEMA,"intent_id":"i-1","candidate_fingerprint":"candidate","evidence_fingerprint":"evidence","account_fingerprint":"account","symbol":"EURUSD","side":"BUY","quantity":"0.01","stop_price":"1.1","target_price":"1.2","quote_timestamp":(NOW-timedelta(seconds=1)).isoformat(),"expires_at":(NOW+timedelta(seconds=30)).isoformat(),"risk_fingerprint":"risk"}
def test_version_identity_and_roundtrip():
    intent=ScalperLiveIntent.parse(payload(),now=NOW)
    assert intent.matches(candidate_fingerprint="candidate",evidence_fingerprint="evidence",account_fingerprint="account",symbol="EURUSD")
    assert not intent.matches(candidate_fingerprint="other",evidence_fingerprint="evidence",account_fingerprint="account",symbol="EURUSD")
    assert ScalperLiveIntent.parse(intent.as_payload(),now=NOW)==intent
def test_malformed_expired_and_future_quotes_refuse():
    with pytest.raises(IntentError,match="wrong_schema"): ScalperLiveIntent.parse({**payload(),"schema":"bad"},now=NOW)
    with pytest.raises(IntentError,match="expired"): ScalperLiveIntent.parse({**payload(),"expires_at":NOW.isoformat()},now=NOW)
    with pytest.raises(IntentError,match="future_quote"): ScalperLiveIntent.parse({**payload(),"quote_timestamp":(NOW+timedelta(seconds=1)).isoformat()},now=NOW)
    with pytest.raises(IntentError,match="invalid_quantity"): ScalperLiveIntent.parse({**payload(),"quantity":"0"},now=NOW)
