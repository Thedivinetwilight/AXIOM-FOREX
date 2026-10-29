"""Explicit financing/rollover accrual with configurable triple-rollover day."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.accounting.rollover import RolloverPolicy
from app.accounting.fx import ConversionRateBook
from app.common.validation import ValidationResult, as_utc, canonical_pair, parse_decimal
from app.config.settings import AccountProfile

from .fx import MissingConversionRate, account_rate


@dataclass(frozen=True)
class SwapRateCard:
    pair: str
    long_rate: Decimal
    short_rate: Decimal
    source: str
    version: str

    @property
    def canonical_pair(self) -> str | None:
        pair, _ = canonical_pair(self.pair)
        return pair

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        pair, pair_error = canonical_pair(self.pair)
        if pair_error or pair is None:
            errors.append(pair_error or "invalid_pair")
        for name in ("long_rate", "short_rate"):
            value, error = parse_decimal(getattr(self, name), name)
            if error or value is None or not value.is_finite():
                errors.append(error or f"non_finite_{name}")
        for name in ("source", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"missing_{name}")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.canonical_pair,
            "long_rate": self.long_rate,
            "short_rate": self.short_rate,
            "source": self.source,
            "version": self.version,
        }


@dataclass(frozen=True)
class RolloverEvent:
    cutoff_at: datetime
    multiplier: int
    swap_rate: Decimal
    financing_quote: Decimal
    conversion_rate: Decimal
    financing_account: Decimal
    triple: bool

    def audit_view(self) -> dict[str, Any]:
        return {
            "cutoff_at": self.cutoff_at,
            "multiplier": self.multiplier,
            "swap_rate": self.swap_rate,
            "financing_quote": self.financing_quote,
            "conversion_rate": self.conversion_rate,
            "financing_account": self.financing_account,
            "triple": self.triple,
        }


@dataclass(frozen=True)
class RolloverAccrual:
    total_quote: Decimal
    total_account: Decimal
    events: tuple[RolloverEvent, ...]

    def audit_view(self) -> dict[str, Any]:
        return {
            "total_quote": self.total_quote,
            "total_account": self.total_account,
            "events": [event.audit_view() for event in self.events],
        }


class RolloverEngine:
    """Accrues financing only at explicit broker-supplied cutoff instants."""

    def __init__(
        self,
        *,
        policy: RolloverPolicy,
        cards: Mapping[str, SwapRateCard],
        conversion_rates: ConversionRateBook,
        account: AccountProfile,
        calendar_id: str,
    ):
        self.policy = policy
        self.cards = dict(cards)
        self.conversion_rates = conversion_rates
        self.account = account
        self.calendar_id = calendar_id

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        errors.extend(self.policy.validate().errors)
        if self.calendar_id != self.policy.calendar_id:
            errors.append("calendar_mismatch")
        if not self.cards:
            errors.append("missing_swap_rate_cards")
        for pair, card in self.cards.items():
            canonical, error = canonical_pair(pair)
            if error or canonical is None or canonical != card.canonical_pair:
                errors.append(f"swap_card_key_mismatch:{pair}")
            errors.extend(card.validate().errors)
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def accrue(
        self,
        position: Any,
        *,
        cutoffs: Sequence[datetime],
        as_of: datetime,
    ) -> RolloverAccrual:
        validation = self.validate()
        if not validation.valid:
            raise ValueError("Invalid rollover engine: " + validation.reason)
        as_of_utc, as_of_error = as_utc(as_of, "as_of")
        entry_utc, entry_error = as_utc(position.entry_timestamp, "entry_timestamp")
        if as_of_error or as_of_utc is None or entry_error or entry_utc is None:
            raise ValueError("invalid_rollover_timestamps")
        if as_of_utc < entry_utc:
            raise ValueError("as_of_before_entry")
        pair, pair_error = canonical_pair(position.pair)
        if pair_error or pair is None:
            raise ValueError("invalid_position_pair")
        card = self.cards.get(pair)
        if card is None:
            raise ValueError("missing_swap_rate_card")
        side_value = position.side.value if hasattr(position.side, "value") else str(position.side)
        if side_value not in {"BUY", "SELL"}:
            raise ValueError("invalid_position_side")
        swap_rate = card.long_rate if side_value == "BUY" else card.short_rate
        quote_currency = pair.split("/")[1]
        notional_quote = position.base_units * position.entry_price
        events: list[RolloverEvent] = []
        total_quote = Decimal(0)
        total_account = Decimal(0)
        for raw_cutoff in cutoffs:
            cutoff, cutoff_error = as_utc(raw_cutoff, "cutoff")
            if cutoff_error or cutoff is None:
                raise ValueError("invalid_rollover_cutoff")
            if cutoff > as_of_utc:
                raise ValueError("future_rollover_cutoff")
            if cutoff <= entry_utc:
                continue
            triple = (
                self.policy.triple_rollover_weekday is not None
                and cutoff.weekday() == self.policy.triple_rollover_weekday
            )
            multiplier = 3 if triple else 1
            financing_quote = notional_quote * swap_rate * Decimal(multiplier)
            try:
                conversion_rate = account_rate(
                    conversion_rates=self.conversion_rates,
                    account=self.account,
                    currency=quote_currency,
                    at=cutoff,
                )
            except MissingConversionRate as exc:
                raise ValueError("missing_rollover_conversion_rate") from exc
            financing_account = financing_quote * conversion_rate
            event = RolloverEvent(
                cutoff_at=cutoff,
                multiplier=multiplier,
                swap_rate=swap_rate,
                financing_quote=financing_quote,
                conversion_rate=conversion_rate,
                financing_account=financing_account,
                triple=triple,
            )
            events.append(event)
            total_quote += financing_quote
            total_account += financing_account
        return RolloverAccrual(total_quote, total_account, tuple(events))
