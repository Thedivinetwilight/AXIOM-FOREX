"""Deterministic offline fake adapters for provider/broker conformance.

These adapters replay explicitly labeled local data.  They never contact a
provider, read credentials, or place an external order.  Their failure modes
are deliberate so conformance checks can prove that fail-closed behavior is
preserved across restart and reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from app.audit.log import sanitize_audit_value
from app.calendar.trading import TradingCalendar
from app.common.validation import as_utc, parse_decimal
from app.data.models import ForexQuote, ForexQuoteBar, QuoteFreshness
from app.execution.broker import BrokerCapabilities
from app.risk.models import GateContext, OrderRequest
from app.universe.specs import CurrencyPairSpec

from axiom_forex.data.quality import assess_bars

from .contracts import BrokerConformanceCapabilities


FINAL_ORDER_STATUSES = frozenset({"FILLED", "REJECTED", "CANCELLED"})
UNRESOLVED_ORDER_STATUSES = frozenset({"NEW", "UNKNOWN", "PARTIALLY_FILLED"})


class ConformanceOrderStatus(str, Enum):
    NEW = "NEW"
    UNKNOWN = "UNKNOWN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


def _decimal(value: Any, name: str) -> Decimal:
    parsed, error = parse_decimal(value, name)
    if error or parsed is None:
        raise ValueError(error or f"invalid_{name}")
    return parsed


def _timestamp(value: Any, name: str) -> datetime:
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid_{name}") from exc
    parsed_utc, error = as_utc(parsed, name)
    if error or parsed_utc is None:
        raise ValueError(error or f"invalid_{name}")
    return parsed_utc


def _clean(view: dict[str, Any]) -> dict[str, Any]:
    result = sanitize_audit_value(view)
    return result if isinstance(result, dict) else {}


@dataclass(frozen=True)
class MarketDataScenario:
    provider_symbol: str
    pair: str
    evaluated_at: datetime
    bid_ask_quote: ForexQuote
    stale_quote: ForexQuote
    crossed_quote: ForexQuote
    incomplete_bar: ForexQuoteBar
    gap_bars: tuple[ForexQuoteBar, ...]
    quote_correction: Mapping[str, Any]
    calendar_states: tuple[Mapping[str, Any], ...]
    dst_timezone: str
    dst_before_local: datetime
    dst_after_local: datetime
    dst_before_utc_offset_seconds: int
    dst_after_utc_offset_seconds: int

    def scenario_view(
        self,
        *,
        spec: CurrencyPairSpec,
        freshness: QuoteFreshness,
        calendar: TradingCalendar,
    ) -> dict[str, Any]:
        spread_pips = self.bid_ask_quote.spread_pips(spec)
        stale = freshness.check(self.stale_quote, self.evaluated_at)
        gap_report = assess_bars(
            self.gap_bars,
            spec=spec,
            calendar=calendar,
            freshness=freshness,
            evaluated_at=self.gap_bars[-1].end_at if self.gap_bars else self.evaluated_at,
        )
        correction_timestamp = self.quote_correction.get("timestamp")
        original_bid = self.quote_correction.get("original_bid")
        corrected_bid = self.quote_correction.get("corrected_bid")
        return _clean(
            {
                "provider_symbol": self.provider_symbol,
                "pair": self.pair,
                "evaluated_at": self.evaluated_at,
                "bid_ask": {
                    "bid": self.bid_ask_quote.bid,
                    "ask": self.bid_ask_quote.ask,
                    "spread_pips": spread_pips,
                    "valid": self.bid_ask_quote.validate(spec).valid,
                    "fresh": freshness.check(
                        self.bid_ask_quote, self.evaluated_at
                    ).ok,
                },
                "stale_quote": {
                    "ok": stale.ok,
                    "reason": stale.reason.value,
                    "age_seconds": stale.age_seconds,
                },
                "crossed_quote_valid": self.crossed_quote.validate(spec).valid,
                "incomplete_bar_valid": self.incomplete_bar.validate(spec).valid,
                "gap": {
                    "bar_count": gap_report.bar_count,
                    "classification": gap_report.classification,
                    "usable": gap_report.usable,
                    "finding_codes": [
                        finding.code for finding in gap_report.findings
                    ],
                    "interpolation_used": False,
                },
                "quote_correction": {
                    "original_quote_id": self.quote_correction.get("original_quote_id"),
                    "corrected_quote_id": self.quote_correction.get("corrected_quote_id"),
                    "timestamp": correction_timestamp,
                    "original_bid": original_bid,
                    "corrected_bid": corrected_bid,
                    "same_timestamp": bool(correction_timestamp),
                    "price_changed": original_bid != corrected_bid,
                    "policy": self.quote_correction.get("policy"),
                    "corrected_valid": self.quote_correction.get("corrected_valid"),
                },
                "calendar_states": [dict(item) for item in self.calendar_states],
                "dst": {
                    "timezone": self.dst_timezone,
                    "before_local": self.dst_before_local,
                    "after_local": self.dst_after_local,
                    "before_utc_offset_seconds": self.dst_before_utc_offset_seconds,
                    "after_utc_offset_seconds": self.dst_after_utc_offset_seconds,
                    "offset_changed": self.dst_before_utc_offset_seconds
                    != self.dst_after_utc_offset_seconds,
                },
            }
        )


@dataclass(frozen=True)
class DeterministicMarketDataAdapter:
    """Read-only replay adapter implementing the M1 provider protocol shape."""

    provider_id: str
    supported_timeframes: frozenset[str]
    bars: tuple[ForexQuoteBar, ...]
    quotes: tuple[ForexQuote, ...]
    raw_bar_payloads: tuple[Mapping[str, Any], ...]
    raw_quote_payloads: tuple[Mapping[str, Any], ...]
    scenarios: MarketDataScenario

    def latest_quote(self, provider_symbol: str) -> ForexQuote | None:
        if provider_symbol != self.scenarios.provider_symbol or not self.quotes:
            return None
        quote = self.quotes[-1]
        if (quote.canonical_pair or "") != self.scenarios.pair:
            return None
        return quote

    def historical_bars(
        self,
        provider_symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> tuple[ForexQuoteBar, ...]:
        if provider_symbol != self.scenarios.provider_symbol:
            return ()
        if timeframe not in self.supported_timeframes:
            return ()
        start_utc, start_error = as_utc(start, "start")
        end_utc, end_error = as_utc(end, "end")
        if start_error or end_error or start_utc is None or end_utc is None:
            return ()
        if end_utc <= start_utc:
            return ()
        return tuple(
            bar
            for bar in self.bars
            if start_utc <= _timestamp(bar.timestamp, "bar_timestamp") < end_utc
        )


@dataclass(frozen=True)
class ConformanceOrderTransition:
    event_id: str
    status: str
    at: datetime
    detail: str
    cumulative_filled_quantity: Decimal

    def audit_view(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "status": self.status,
            "at": self.at,
            "detail": self.detail,
            "cumulative_filled_quantity": self.cumulative_filled_quantity,
        }

    @classmethod
    def from_audit_view(cls, view: Mapping[str, Any]) -> "ConformanceOrderTransition":
        return cls(
            event_id=str(view.get("event_id", "")),
            status=str(view.get("status", "")).upper(),
            at=_timestamp(view.get("at"), "transition_at"),
            detail=str(view.get("detail", "")),
            cumulative_filled_quantity=_decimal(
                view.get("cumulative_filled_quantity"), "cumulative_filled_quantity"
            ),
        )


@dataclass(frozen=True)
class ConformanceOrderRecord:
    client_order_id: str
    broker_order_id: str
    request_id: str
    provider_id: str
    provider_symbol: str
    pair: str
    side: str
    quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    status: str
    reason: str
    created_at: datetime
    updated_at: datetime
    transitions: tuple[ConformanceOrderTransition, ...]
    request_fingerprint: str

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_unresolved(self) -> bool:
        return self.status in UNRESOLVED_ORDER_STATUSES

    def audit_view(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "provider_symbol": self.provider_symbol,
            "pair": self.pair,
            "side": self.side,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "average_price": self.average_price,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "transitions": [item.audit_view() for item in self.transitions],
            "request_fingerprint": self.request_fingerprint,
            "is_unresolved": self.is_unresolved,
        }

    @classmethod
    def from_audit_view(cls, view: Mapping[str, Any]) -> "ConformanceOrderRecord":
        transitions_view = view.get("transitions", ())
        if not isinstance(transitions_view, Sequence):
            raise ValueError("invalid_transitions")
        transitions = tuple(
            ConformanceOrderTransition.from_audit_view(item)
            for item in transitions_view
            if isinstance(item, Mapping)
        )
        quantity = _decimal(view.get("quantity"), "quantity")
        filled_quantity = _decimal(view.get("filled_quantity"), "filled_quantity")
        if filled_quantity > quantity:
            raise ValueError("filled_quantity_above_quantity")
        average_raw = view.get("average_price")
        average_price = None if average_raw in (None, "") else _decimal(average_raw, "average_price")
        return cls(
            client_order_id=str(view.get("client_order_id", "")),
            broker_order_id=str(view.get("broker_order_id", "")),
            request_id=str(view.get("request_id", "")),
            provider_id=str(view.get("provider_id", "")),
            provider_symbol=str(view.get("provider_symbol", "")),
            pair=str(view.get("pair", "")),
            side=str(view.get("side", "")).upper(),
            quantity=quantity,
            filled_quantity=filled_quantity,
            average_price=average_price,
            status=str(view.get("status", "")).upper(),
            reason=str(view.get("reason", "")),
            created_at=_timestamp(view.get("created_at"), "created_at"),
            updated_at=_timestamp(view.get("updated_at"), "updated_at"),
            transitions=transitions,
            request_fingerprint=str(view.get("request_fingerprint", "")),
        )


def _side_value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value).upper()


def _fingerprint(request: OrderRequest) -> str:
    quantity, error = parse_decimal(request.quantity, "quantity")
    if error or quantity is None:
        quantity = Decimal(0)
    evaluated = _timestamp(request.evaluated_at, "evaluated_at")
    quote = request.quote
    quote_id = str(getattr(quote, "quote_id", "") or "")
    bid = str(getattr(quote, "bid", "") or "")
    ask = str(getattr(quote, "ask", "") or "")
    return "|".join(
        (
            request.request_id,
            request.provider_id,
            request.provider_symbol,
            _side_value(request.side),
            str(quantity),
            evaluated.isoformat(),
            quote_id,
            bid,
            ask,
        )
    )


class DeterministicBrokerAdapter:
    """Local order-lifecycle replay with explicit idempotency and recovery."""

    def __init__(
        self,
        *,
        capabilities: BrokerConformanceCapabilities,
        m1_capabilities: BrokerCapabilities,
    ) -> None:
        self.capabilities = capabilities
        self.m1_capabilities = m1_capabilities
        self._orders: dict[str, ConformanceOrderRecord] = {}
        self._by_broker_id: dict[str, str] = {}
        self._applied_event_ids: set[str] = set()
        self._event_counter = 0
        self._external_calls = 0
        self._resubmit_blocked_count = 0
        self._idempotency_conflicts = 0

    def _event_id(self, label: str) -> str:
        self._event_counter += 1
        return f"{self.capabilities.broker_id}:{label}:{self._event_counter}"

    def _store(self, record: ConformanceOrderRecord) -> ConformanceOrderRecord:
        self._orders[record.client_order_id] = record
        self._by_broker_id[record.broker_order_id] = record.client_order_id
        for transition in record.transitions:
            self._applied_event_ids.add(transition.event_id)
        return record

    def _base_record(
        self,
        *,
        client_order_id: str,
        request: OrderRequest,
        submitted_at: datetime,
        quantity: Decimal,
    ) -> ConformanceOrderRecord:
        at = _timestamp(submitted_at, "submitted_at")
        quote = request.quote
        pair = str(getattr(quote, "canonical_pair", "") or getattr(quote, "pair", "") or "")
        broker_order_id = f"{self.capabilities.broker_id}:{client_order_id}"
        transition = ConformanceOrderTransition(
            event_id=self._event_id("submitted"),
            status=ConformanceOrderStatus.NEW.value,
            at=at,
            detail="LOCAL_SIMULATED_INTENT",
            cumulative_filled_quantity=Decimal(0),
        )
        return ConformanceOrderRecord(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            request_id=request.request_id,
            provider_id=request.provider_id,
            provider_symbol=request.provider_symbol,
            pair=pair,
            side=_side_value(request.side),
            quantity=quantity,
            filled_quantity=Decimal(0),
            average_price=None,
            status=ConformanceOrderStatus.NEW.value,
            reason="",
            created_at=at,
            updated_at=at,
            transitions=(transition,),
            request_fingerprint=_fingerprint(request),
        )

    def _with_transition(
        self,
        record: ConformanceOrderRecord,
        *,
        status: str,
        at: datetime,
        detail: str,
        reason: str = "",
        event_id: str | None = None,
        cumulative_filled_quantity: Decimal | None = None,
    ) -> ConformanceOrderRecord:
        transition_id = event_id or self._event_id(status.lower())
        if transition_id in self._applied_event_ids:
            return record
        timestamp = _timestamp(at, "transition_at")
        transition = ConformanceOrderTransition(
            event_id=transition_id,
            status=status,
            at=timestamp,
            detail=detail,
            cumulative_filled_quantity=(
                cumulative_filled_quantity
                if cumulative_filled_quantity is not None
                else record.filled_quantity
            ),
        )
        updated = replace(
            record,
            status=status,
            reason=reason or record.reason,
            updated_at=timestamp,
            transitions=record.transitions + (transition,),
        )
        return self._store(updated)

    def submit_order(
        self,
        *,
        client_order_id: str,
        request: OrderRequest,
        submitted_at: datetime,
        initial_status: str = "NEW",
        reason: str = "",
        quantity: Decimal | None = None,
    ) -> ConformanceOrderRecord:
        existing = self._orders.get(client_order_id)
        fingerprint = _fingerprint(request)
        if existing is not None:
            self._resubmit_blocked_count += 1
            if existing.request_fingerprint != fingerprint:
                self._idempotency_conflicts += 1
                # Return a rejected response without mutating the existing order.
                return replace(
                    existing,
                    status=ConformanceOrderStatus.REJECTED.value,
                    reason="IDEMPOTENCY_CONFLICT",
                )
            return existing
        order_quantity, error = parse_decimal(
            quantity if quantity is not None else request.quantity, "quantity"
        )
        if error or order_quantity is None or order_quantity <= 0:
            raise ValueError(error or "invalid_quantity")
        record = self._base_record(
            client_order_id=client_order_id,
            request=request,
            submitted_at=submitted_at,
            quantity=order_quantity,
        )
        self._store(record)
        status = str(initial_status).upper()
        at = _timestamp(submitted_at, "submitted_at") + timedelta(milliseconds=1)
        if status == ConformanceOrderStatus.UNKNOWN.value:
            return self._with_transition(
                record,
                status=status,
                at=at,
                detail="UNKNOWN_ORDER_OUTCOME",
                reason=reason or "UNKNOWN_OUTCOME",
            )
        if status == ConformanceOrderStatus.REJECTED.value:
            return self._with_transition(
                record,
                status=status,
                at=at,
                detail="PROVIDER_REJECTION",
                reason=reason or "BROKER_REJECTED",
            )
        if status == ConformanceOrderStatus.FILLED.value:
            price = self._request_price(request)
            return self.apply_fill(
                client_order_id,
                incremental_quantity=order_quantity,
                price=price,
                at=at,
                event_id=self._event_id("initial-fill"),
            )
        if status != ConformanceOrderStatus.NEW.value:
            raise ValueError("unsupported_initial_status")
        return record

    @staticmethod
    def _request_price(request: OrderRequest) -> Decimal:
        quote = request.quote
        if quote is None:
            raise ValueError("missing_quote")
        if hasattr(quote, "executable_price"):
            return quote.executable_price(request.side)
        side = _side_value(request.side)
        raw = getattr(quote, "ask" if side == "BUY" else "bid", None)
        return _decimal(raw, "price")

    def apply_fill(
        self,
        client_order_id: str,
        *,
        incremental_quantity: Decimal,
        price: Decimal,
        at: datetime,
        event_id: str | None = None,
    ) -> ConformanceOrderRecord:
        record = self._orders.get(client_order_id)
        if record is None:
            raise ValueError("unknown_client_order_id")
        fill_quantity = _decimal(incremental_quantity, "incremental_quantity")
        fill_price = _decimal(price, "price")
        transition_id = event_id or self._event_id("fill")
        if transition_id in self._applied_event_ids:
            return record
        if fill_quantity <= 0:
            raise ValueError("non_positive_fill_quantity")
        if record.status in FINAL_ORDER_STATUSES:
            raise ValueError("fill_after_final_status")
        new_filled = record.filled_quantity + fill_quantity
        if new_filled > record.quantity:
            raise ValueError("fill_quantity_above_order_quantity")
        existing_notional = (
            record.average_price * record.filled_quantity
            if record.average_price is not None
            else Decimal(0)
        )
        average_price = (existing_notional + fill_price * fill_quantity) / new_filled
        status = (
            ConformanceOrderStatus.FILLED.value
            if new_filled == record.quantity
            else ConformanceOrderStatus.PARTIALLY_FILLED.value
        )
        timestamp = _timestamp(at, "fill_at")
        transition = ConformanceOrderTransition(
            event_id=transition_id,
            status=status,
            at=timestamp,
            detail="INCREMENTAL_FILL",
            cumulative_filled_quantity=new_filled,
        )
        updated = replace(
            record,
            filled_quantity=new_filled,
            average_price=average_price,
            status=status,
            updated_at=timestamp,
            transitions=record.transitions + (transition,),
        )
        return self._store(updated)

    def reject(
        self, client_order_id: str, *, reason: str, at: datetime
    ) -> ConformanceOrderRecord:
        record = self._orders.get(client_order_id)
        if record is None:
            raise ValueError("unknown_client_order_id")
        if record.status in FINAL_ORDER_STATUSES:
            return record
        return self._with_transition(
            record,
            status=ConformanceOrderStatus.REJECTED.value,
            at=at,
            detail="REJECTION_CONFIRMED",
            reason=reason,
        )

    def cancel(
        self, client_order_id: str, *, at: datetime, reason: str = "CANCEL_CONFIRMED"
    ) -> ConformanceOrderRecord:
        record = self._orders.get(client_order_id)
        if record is None:
            raise ValueError("unknown_client_order_id")
        if record.status in FINAL_ORDER_STATUSES:
            return record
        return self._with_transition(
            record,
            status=ConformanceOrderStatus.CANCELLED.value,
            at=at,
            detail="CANCELLATION_CONFIRMED",
            reason=reason,
        )

    def reconcile_unknown(
        self,
        client_order_id: str,
        *,
        resolution: str,
        at: datetime,
        price: Decimal | None = None,
    ) -> ConformanceOrderRecord:
        record = self._orders.get(client_order_id)
        if record is None:
            raise ValueError("unknown_client_order_id")
        final = str(resolution).upper()
        if final not in FINAL_ORDER_STATUSES:
            raise ValueError("invalid_reconciliation_resolution")
        if record.status != ConformanceOrderStatus.UNKNOWN.value:
            return record
        timestamp = _timestamp(at, "reconciled_at")
        if final == ConformanceOrderStatus.FILLED.value:
            fill_price = price if price is not None else Decimal("1.10020")
            return self.apply_fill(
                client_order_id,
                incremental_quantity=record.remaining_quantity,
                price=fill_price,
                at=timestamp,
                event_id=self._event_id("reconciled-fill"),
            )
        return self._with_transition(
            record,
            status=final,
            at=timestamp,
            detail="RECONCILED_ORDER_HISTORY",
            reason=final,
        )

    def get_order(self, client_order_id: str) -> ConformanceOrderRecord | None:
        return self._orders.get(client_order_id)

    def get_order_by_broker_id(self, broker_order_id: str) -> ConformanceOrderRecord | None:
        client_order_id = self._by_broker_id.get(broker_order_id)
        return self._orders.get(client_order_id) if client_order_id else None

    def list_orders(self) -> tuple[ConformanceOrderRecord, ...]:
        return tuple(
            sorted(self._orders.values(), key=lambda item: item.client_order_id)
        )

    def unresolved_orders(self) -> tuple[ConformanceOrderRecord, ...]:
        return tuple(item for item in self.list_orders() if item.is_unresolved)

    def simulate_order_intent(
        self, request: OrderRequest, context: GateContext
    ) -> dict[str, Any]:
        """Create a local simulated intent; no external order is ever sent."""
        del context
        record = self.submit_order(
            client_order_id=request.request_id,
            request=request,
            submitted_at=request.evaluated_at,
            initial_status="NEW",
        )
        return _clean(
            {
                "simulation_id": record.broker_order_id,
                "status": "SIMULATED_INTENT",
                "live": False,
                "mode": "PAPER",
                "order": record.audit_view(),
            }
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "broker_id": self.capabilities.broker_id,
            "capability_version": self.capabilities.capability_version,
            "orders": [item.audit_view() for item in self.list_orders()],
            "applied_event_ids": sorted(self._applied_event_ids),
            "resubmit_blocked_count": self._resubmit_blocked_count,
            "idempotency_conflicts": self._idempotency_conflicts,
            "external_calls": self._external_calls,
        }

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        if snapshot.get("broker_id") != self.capabilities.broker_id:
            raise ValueError("snapshot_broker_mismatch")
        if snapshot.get("capability_version") != self.capabilities.capability_version:
            raise ValueError("snapshot_version_mismatch")
        orders_view = snapshot.get("orders", ())
        if not isinstance(orders_view, Sequence):
            raise ValueError("invalid_snapshot_orders")
        recovered: dict[str, ConformanceOrderRecord] = {}
        by_broker: dict[str, str] = {}
        for view in orders_view:
            if not isinstance(view, Mapping):
                raise ValueError("invalid_snapshot_order")
            record = ConformanceOrderRecord.from_audit_view(view)
            if record.client_order_id in recovered:
                raise ValueError("duplicate_snapshot_client_order_id")
            recovered[record.client_order_id] = record
            by_broker[record.broker_order_id] = record.client_order_id
        event_ids = snapshot.get("applied_event_ids", ())
        if not isinstance(event_ids, Sequence):
            raise ValueError("invalid_snapshot_event_ids")
        applied = {str(item) for item in event_ids}
        transition_ids = {
            transition.event_id
            for record in recovered.values()
            for transition in record.transitions
        }
        if not transition_ids.issubset(applied):
            raise ValueError("snapshot_transition_ids_missing")
        self._orders = recovered
        self._by_broker_id = by_broker
        self._applied_event_ids = applied
        self._event_counter = len(applied)
        self._resubmit_blocked_count = int(snapshot.get("resubmit_blocked_count", 0))
        self._idempotency_conflicts = int(snapshot.get("idempotency_conflicts", 0))
        self._external_calls = int(snapshot.get("external_calls", 0))

    def fixture_scenarios(self) -> dict[str, Any]:
        """Return a deterministic replay of realistic offline lifecycle failures."""
        from fixtures.m1 import make_request

        clone = DeterministicBrokerAdapter(
            capabilities=self.capabilities,
            m1_capabilities=self.m1_capabilities,
        )
        request = make_request()
        conflict_request = replace(request, request_id="conformance-conflict-request")
        base_at = datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
        price = Decimal("1.10020")

        filled = clone.submit_order(
            client_order_id="conformance-idempotent",
            request=request,
            submitted_at=base_at,
            initial_status="FILLED",
        )
        duplicate = clone.submit_order(
            client_order_id="conformance-idempotent",
            request=request,
            submitted_at=base_at + timedelta(seconds=1),
            initial_status="FILLED",
        )
        conflict = clone.submit_order(
            client_order_id="conformance-idempotent",
            request=conflict_request,
            submitted_at=base_at + timedelta(seconds=2),
            initial_status="FILLED",
        )

        rejected = clone.submit_order(
            client_order_id="conformance-rejected",
            request=request,
            submitted_at=base_at + timedelta(seconds=3),
            initial_status="REJECTED",
            reason="BROKER_REJECTED",
        )

        clone.submit_order(
            client_order_id="conformance-partial",
            request=request,
            submitted_at=base_at + timedelta(seconds=4),
            initial_status="NEW",
            quantity=Decimal("0.10"),
        )
        partial = clone.apply_fill(
            "conformance-partial",
            incremental_quantity=Decimal("0.04"),
            price=price,
            at=base_at + timedelta(seconds=5),
            event_id="conformance-partial-fill-1",
        )
        duplicate_fill = clone.apply_fill(
            "conformance-partial",
            incremental_quantity=Decimal("0.04"),
            price=price,
            at=base_at + timedelta(seconds=6),
            event_id="conformance-partial-fill-1",
        )
        cancelled_partial = clone.cancel(
            "conformance-partial",
            at=base_at + timedelta(seconds=7),
            reason="CANCEL_CONFIRMED",
        )

        unknown = clone.submit_order(
            client_order_id="conformance-unknown-resubmit",
            request=request,
            submitted_at=base_at + timedelta(seconds=8),
            initial_status="UNKNOWN",
            reason="UNKNOWN_OUTCOME",
        )
        unknown_resubmit = clone.submit_order(
            client_order_id="conformance-unknown-resubmit",
            request=request,
            submitted_at=base_at + timedelta(seconds=9),
            initial_status="FILLED",
        )

        clone.submit_order(
            client_order_id="conformance-unknown-reconciled",
            request=request,
            submitted_at=base_at + timedelta(seconds=10),
            initial_status="UNKNOWN",
            reason="UNKNOWN_OUTCOME",
        )
        reconciled = clone.reconcile_unknown(
            "conformance-unknown-reconciled",
            resolution="FILLED",
            at=base_at + timedelta(seconds=11),
            price=price,
        )

        clone.submit_order(
            client_order_id="conformance-restart-unresolved",
            request=request,
            submitted_at=base_at + timedelta(seconds=12),
            initial_status="UNKNOWN",
            reason="UNKNOWN_OUTCOME",
        )
        snapshot = clone.snapshot()
        restarted = DeterministicBrokerAdapter(
            capabilities=self.capabilities,
            m1_capabilities=self.m1_capabilities,
        )
        restarted.restore(snapshot)
        restart_recovery_equal = restarted.snapshot() == snapshot
        unresolved_before = tuple(item.client_order_id for item in clone.unresolved_orders())
        unresolved_after = tuple(
            item.client_order_id for item in restarted.unresolved_orders()
        )
        restart_resubmit = restarted.submit_order(
            client_order_id="conformance-restart-unresolved",
            request=request,
            submitted_at=base_at + timedelta(seconds=13),
            initial_status="FILLED",
        )
        restarted_record = restarted.get_order("conformance-restart-unresolved")
        all_records = clone.list_orders()
        transition_ids = [
            transition.event_id
            for record in all_records
            for transition in record.transitions
        ]
        return _clean(
            {
                "broker_id": self.capabilities.broker_id,
                "capability_version": self.capabilities.capability_version,
                "execution_mode": self.capabilities.execution_mode_value,
                "external_orders_supported": self.capabilities.external_orders_supported,
                "external_calls": clone._external_calls,
                "order_types": sorted(self.capabilities.order_types),
                "simulated_intent_supported": "SIMULATED_INTENT" in self.capabilities.order_types,
                "idempotency_key": self.capabilities.idempotency_key,
                "idempotency_duplicate_same_id": duplicate.broker_order_id == filled.broker_order_id,
                "idempotency_transition_count_unchanged": len(duplicate.transitions)
                == len(filled.transitions),
                "idempotency_conflict_rejected": conflict.status
                == ConformanceOrderStatus.REJECTED.value,
                "idempotency_conflict_reason": conflict.reason,
                "no_resubmit_while_unresolved": unknown_resubmit.status
                == ConformanceOrderStatus.UNKNOWN.value
                and len(unknown_resubmit.transitions) == len(unknown.transitions),
                "resubmit_blocked_count": clone._resubmit_blocked_count,
                "unknown_outcome_policy": self.capabilities.unknown_outcome_policy,
                "rejected": rejected.audit_view(),
                "partial": partial.audit_view(),
                "partial_duplicate_fill_noop": duplicate_fill.filled_quantity
                == partial.filled_quantity
                and len(duplicate_fill.transitions) == len(partial.transitions),
                "cancelled_partial": cancelled_partial.audit_view(),
                "unknown": unknown.audit_view(),
                "unknown_reconciled": reconciled.audit_view(),
                "reconcile_status_sequence": [
                    transition.status for transition in reconciled.transitions
                ],
                "unresolved_order_ids": list(unresolved_before),
                "restart_recovery_equal": restart_recovery_equal,
                "restart_unresolved_equal": unresolved_before == unresolved_after,
                "restart_no_resubmit_unresolved": restart_resubmit.status
                == ConformanceOrderStatus.UNKNOWN.value
                and restarted_record is not None
                and restarted_record.is_unresolved,
                "snapshot_order_count": len(snapshot["orders"]),
                "all_transitions_unique": len(transition_ids) == len(set(transition_ids)),
                "all_external_orders_disabled": (
                    not self.capabilities.external_orders_supported
                    and not self.capabilities.supports_live_orders
                    and not self.m1_capabilities.supports_live_orders
                    and clone._external_calls == 0
                ),
            }
        )

    def scenario_view(self) -> dict[str, Any]:
        return self.fixture_scenarios()
