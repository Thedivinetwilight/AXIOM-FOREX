"""Versioned, fail-closed AX-SCALPER LIVE proposal envelope.

No broker interfaces are imported here: a proposal can only be consumed by the
existing /live review workflow, never executed by this module.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

SCHEMA = "axiom-scalper-live-intent-v1"

class IntentError(ValueError):
    pass

def _positive(value: Any, field: str) -> Decimal:
    try: result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError): raise IntentError(f"invalid_{field}") from None
    if not result.is_finite() or result <= 0: raise IntentError(f"invalid_{field}")
    return result

@dataclass(frozen=True)
class ScalperLiveIntent:
    intent_id: str
    candidate_fingerprint: str
    evidence_fingerprint: str
    account_fingerprint: str
    symbol: str
    side: str
    quantity: Decimal
    stop_price: Decimal
    target_price: Decimal
    quote_timestamp: datetime
    expires_at: datetime
    risk_fingerprint: str
    schema: str = SCHEMA

    @classmethod
    def parse(cls, raw: Mapping[str, Any], *, now: datetime | None = None) -> "ScalperLiveIntent":
        if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA: raise IntentError("wrong_schema")
        keys = ("intent_id", "candidate_fingerprint", "evidence_fingerprint", "account_fingerprint", "symbol", "side", "risk_fingerprint", "quote_timestamp", "expires_at", "quantity", "stop_price", "target_price")
        if any(not str(raw.get(k, "") or "").strip() for k in keys): raise IntentError("missing_field")
        side = str(raw["side"]).upper()
        if side not in {"BUY", "SELL"}: raise IntentError("invalid_side")
        def timestamp(key: str) -> datetime:
            try: value = datetime.fromisoformat(str(raw[key]).replace("Z", "+00:00"))
            except ValueError: raise IntentError(f"invalid_{key}") from None
            if value.tzinfo is None: raise IntentError(f"invalid_{key}")
            return value.astimezone(timezone.utc)
        quote, expiry = timestamp("quote_timestamp"), timestamp("expires_at")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if expiry <= current: raise IntentError("expired")
        if quote > current: raise IntentError("future_quote")
        return cls(*(str(raw[k]) for k in keys[:5]), side, _positive(raw["quantity"], "quantity"), _positive(raw["stop_price"], "stop_price"), _positive(raw["target_price"], "target_price"), quote, expiry, str(raw["risk_fingerprint"]), SCHEMA)

    def matches(self, *, candidate_fingerprint: str, evidence_fingerprint: str, account_fingerprint: str, symbol: str) -> bool:
        return (self.candidate_fingerprint == candidate_fingerprint and self.evidence_fingerprint == evidence_fingerprint and self.account_fingerprint == account_fingerprint and self.symbol == symbol)

    def as_payload(self) -> dict[str, str]:
        return {"schema":self.schema,"intent_id":self.intent_id,"candidate_fingerprint":self.candidate_fingerprint,"evidence_fingerprint":self.evidence_fingerprint,"account_fingerprint":self.account_fingerprint,"symbol":self.symbol,"side":self.side,"quantity":str(self.quantity),"stop_price":str(self.stop_price),"target_price":str(self.target_price),"quote_timestamp":self.quote_timestamp.isoformat(),"expires_at":self.expires_at.isoformat(),"risk_fingerprint":self.risk_fingerprint}
