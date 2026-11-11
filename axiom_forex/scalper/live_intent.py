"""Versioned AX-SCALPER proposal accepted only by the existing /live workflow.

No broker interfaces are imported. Validation does not imply admission, review,
approval, arming, freshness at submission, or authority to execute.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

SCHEMA = "axiom-scalper-live-intent-v1"
#: v2 adds the declared size unit.  v1 remains readable for stored research
#: proposals, but the LIVE bridge refuses a v1 proposal that has no unit.
SCHEMA_V2 = "axiom-scalper-live-intent-v2"
SCHEMAS = frozenset({SCHEMA, SCHEMA_V2})
QUANTITY_UNITS = frozenset({"lots", "base_units"})


class IntentError(ValueError):
    """A malformed or unsafe proposal; callers must fail closed."""


def _positive(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise IntentError(f"invalid_{field}") from None
    if not number.is_finite() or number <= 0:
        raise IntentError(f"invalid_{field}")
    return number


def _instant(raw: Any, field: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise IntentError(f"invalid_{field}") from None
    if value.tzinfo is None:
        raise IntentError(f"invalid_{field}")
    return value.astimezone(timezone.utc)


def _quantity_unit(value: Any, *, required: bool) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        if required:
            raise IntentError("missing_quantity_unit")
        return ""
    if raw not in QUANTITY_UNITS:
        raise IntentError("invalid_quantity_unit")
    return raw


@dataclass(frozen=True)
class ScalperLiveIntent:
    intent_id: str
    candidate_fingerprint: str
    evidence_fingerprint: str
    account_fingerprint: str
    instrument_fingerprint: str
    symbol: str
    timeframe: str
    side: str
    quantity: Decimal
    stop_price: Decimal
    target_price: Decimal
    quote_bid: Decimal
    quote_ask: Decimal
    quote_timestamp: datetime
    quote_source: str
    expires_at: datetime
    risk_fingerprint: str
    config_fingerprint: str
    quantity_unit: str = ""
    schema: str = SCHEMA

    @classmethod
    def parse(cls, raw: Mapping[str, Any], *, now: datetime | None = None) -> "ScalperLiveIntent":
        if not isinstance(raw, Mapping) or raw.get("schema") not in SCHEMAS:
            raise IntentError("wrong_schema")
        schema = str(raw["schema"])
        text_fields = ("intent_id", "candidate_fingerprint", "evidence_fingerprint", "account_fingerprint",
                       "instrument_fingerprint", "symbol", "timeframe", "side", "quote_source",
                       "risk_fingerprint", "config_fingerprint", "quote_timestamp", "expires_at")
        if any(not str(raw.get(key, "") or "").strip() for key in text_fields):
            raise IntentError("missing_field")
        # A separate reason keeps remediation precise: v2 without a unit is not
        # a generic malformed document, and v1 is never given an invented unit.
        unit = _quantity_unit(raw.get("quantity_unit"), required=schema == SCHEMA_V2)
        side = str(raw["side"]).upper()
        if side not in {"BUY", "SELL"}:
            raise IntentError("invalid_side")
        bid, ask = _positive(raw.get("quote_bid"), "quote_bid"), _positive(raw.get("quote_ask"), "quote_ask")
        if ask < bid:
            raise IntentError("crossed_quote")
        stop, target = _positive(raw.get("stop_price"), "stop_price"), _positive(raw.get("target_price"), "target_price")
        if side == "BUY" and not (stop < bid < ask < target):
            raise IntentError("invalid_buy_protections")
        if side == "SELL" and not (target < bid < ask < stop):
            raise IntentError("invalid_sell_protections")
        quote, expiry = _instant(raw["quote_timestamp"], "quote_timestamp"), _instant(raw["expires_at"], "expires_at")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if quote > current:
            raise IntentError("future_quote")
        if expiry <= current:
            raise IntentError("expired")
        return cls(
            intent_id=str(raw["intent_id"]),
            candidate_fingerprint=str(raw["candidate_fingerprint"]),
            evidence_fingerprint=str(raw["evidence_fingerprint"]),
            account_fingerprint=str(raw["account_fingerprint"]),
            instrument_fingerprint=str(raw["instrument_fingerprint"]),
            symbol=str(raw["symbol"]),
            timeframe=str(raw["timeframe"]),
            side=side,
            quantity=_positive(raw.get("quantity"), "quantity"),
            stop_price=stop,
            target_price=target,
            quote_bid=bid,
            quote_ask=ask,
            quote_timestamp=quote,
            quote_source=str(raw["quote_source"]),
            expires_at=expiry,
            risk_fingerprint=str(raw["risk_fingerprint"]),
            config_fingerprint=str(raw["config_fingerprint"]),
            quantity_unit=unit,
            schema=schema,
        )

    def quote_is_fresh(self, *, now: datetime, max_age_seconds: Decimal | int | float) -> bool:
        """Freshness is re-evaluated by /live at submission; parsing alone is insufficient."""
        current = now.astimezone(timezone.utc)
        return (self.quote_timestamp <= current and
                Decimal(str((current - self.quote_timestamp).total_seconds())) <= Decimal(str(max_age_seconds)))

    def matches(self, *, candidate_fingerprint: str, evidence_fingerprint: str, account_fingerprint: str,
                instrument_fingerprint: str, symbol: str, timeframe: str, risk_fingerprint: str,
                config_fingerprint: str, quantity_unit: str = "") -> bool:
        result = (self.candidate_fingerprint == candidate_fingerprint and self.evidence_fingerprint == evidence_fingerprint
                  and self.account_fingerprint == account_fingerprint and self.instrument_fingerprint == instrument_fingerprint
                  and self.symbol == symbol and self.timeframe == timeframe and self.risk_fingerprint == risk_fingerprint
                  and self.config_fingerprint == config_fingerprint)
        if quantity_unit:
            result = result and self.quantity_unit == _quantity_unit(quantity_unit, required=True)
        return result

    def as_payload(self) -> dict[str, str]:
        payload = {"schema": self.schema, "intent_id": self.intent_id,
                   "candidate_fingerprint": self.candidate_fingerprint, "evidence_fingerprint": self.evidence_fingerprint,
                   "account_fingerprint": self.account_fingerprint, "instrument_fingerprint": self.instrument_fingerprint,
                   "symbol": self.symbol, "timeframe": self.timeframe, "side": self.side, "quantity": str(self.quantity),
                   "stop_price": str(self.stop_price), "target_price": str(self.target_price),
                   "quote_bid": str(self.quote_bid), "quote_ask": str(self.quote_ask),
                   "quote_timestamp": self.quote_timestamp.isoformat(), "quote_source": self.quote_source,
                   "expires_at": self.expires_at.isoformat(), "risk_fingerprint": self.risk_fingerprint,
                   "config_fingerprint": self.config_fingerprint}
        if self.quantity_unit:
            payload["quantity_unit"] = self.quantity_unit
        return payload
