from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from axiom_forex.scalper.live_intent import IntentError, SCHEMA, ScalperLiveIntent

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
