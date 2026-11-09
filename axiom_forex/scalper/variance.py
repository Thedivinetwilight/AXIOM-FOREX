"""VARIANCE: size from the risk that is actually at stake.

The size of a position is decided by one equation:

```
lots = permitted loss ÷ loss per lot at the stop
```

``permitted loss`` is the smaller of the configured fraction of equity and an
absolute cap, scaled down by the regime and by volatility.  ``loss per lot`` comes
from the instrument's own contract size, point and the **stop distance the signal
carries** — so a wider stop automatically buys fewer lots.  That is the difference
between *capital allocated* and *maximum possible loss*, and this module never
confuses them: ``notional`` and ``margin`` are reported, but neither is the risk.

Rounding is always **down** to the broker's volume step.  Rounding up would exceed
the risk budget, which is the one thing this module exists to prevent; when the
budget cannot buy even the minimum volume the trade is refused by name
(``size_below_minimum``) instead of being rounded up "just this once".

Kelly sizing is available as :func:`fractional_kelly` and is **off** by default.
It is a function of a probability, and until a probability has been calibrated
out of sample, feeding it to a sizing formula is a way of laundering an estimate
into a position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Mapping

from .config import InstrumentSpec

#: Refusal reasons.
NO_SPEC = "instrument_terms_missing"
NO_STOP = "stop_distance_not_positive"
NO_RISK_BUDGET = "risk_budget_not_positive"
SIZE_BELOW_MINIMUM = "size_below_the_broker_minimum"
LEVERAGE_CAP = "capped_by_maximum_leverage"
AGGREGATE_RISK_CAP = "capped_by_aggregate_open_risk"
CURRENCY_CAP = "capped_by_currency_concentration"
MARGIN_REFUSED = "insufficient_free_margin"
EQUITY_UNKNOWN = "equity_not_known"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    steps = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return steps * step


@dataclass(frozen=True)
class SizingInput:
    """Everything the calculation reads.  Nothing here is optional-by-accident."""

    spec: InstrumentSpec
    price: Decimal
    stop_points: Decimal
    equity: Decimal
    risk_fraction: Decimal
    max_loss_account: Decimal
    size_multiplier: Decimal = Decimal("1")
    volatility_points: float | None = None
    target_volatility_points: float | None = None
    open_risk_account: Decimal = Decimal("0")
    open_notional_account: Decimal = Decimal("0")
    max_aggregate_risk_fraction: Decimal = Decimal("0.01")
    max_leverage: Decimal = Decimal("5")
    max_currency_exposure_fraction: Decimal = Decimal("0.60")
    currency_exposure: Mapping[str, Decimal] = field(default_factory=dict)
    max_open_positions: int = 2
    open_positions: int = 0
    volume_step: Decimal | None = None
    volume_min: Decimal | None = None
    volume_max: Decimal | None = None


@dataclass(frozen=True)
class Sizing:
    lots: Decimal
    risk_account: Decimal
    notional_account: Decimal
    margin_account: Decimal
    leverage_after: Decimal
    aggregate_risk_after: Decimal
    currency_exposure_after: dict[str, Decimal]
    risk_budget: Decimal
    loss_per_lot: Decimal
    volatility_scale: Decimal
    refusals: tuple[str, ...]
    adjustments: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return not self.refusals

    def audit_view(self) -> dict[str, Any]:
        return {
            "lots": str(self.lots),
            "risk_account": str(round(self.risk_account, 6)),
            "notional_account": str(round(self.notional_account, 2)),
            "margin_account": str(round(self.margin_account, 2)),
            "leverage_after": str(round(self.leverage_after, 4)),
            "aggregate_risk_after": str(round(self.aggregate_risk_after, 6)),
            "risk_budget": str(round(self.risk_budget, 6)),
            "loss_per_lot": str(round(self.loss_per_lot, 6)),
            "volatility_scale": str(round(self.volatility_scale, 4)),
            "refusals": list(self.refusals),
            "adjustments": list(self.adjustments),
            "note": (
                "lots = permitted loss / loss per lot at the stop. Notional and margin "
                "are reported for the margin check; neither is the amount at risk."
            ),
        }


def volatility_scale(
    *,
    volatility_points: float | None,
    target_volatility_points: float | None,
    floor: Decimal = Decimal("0.25"),
) -> Decimal:
    """Reduce size when the market is more volatile than the target, never increase it.

    Scaling *up* in quiet markets is how a variance-based sizer quietly turns into a
    leverage sizer, so the multiplier is capped at 1.
    """

    if not volatility_points or not target_volatility_points:
        return Decimal("1")
    if volatility_points <= 0:
        return Decimal("1")
    raw = Decimal(str(target_volatility_points / volatility_points))
    if raw > 1:
        return Decimal("1")
    return max(floor, raw)


def size_position(request: SizingInput) -> Sizing:
    """Compute a size, or refuse with the reason."""

    spec = request.spec
    refusals: list[str] = []
    adjustments: list[str] = []
    point_value = spec.point_value_per_lot
    equity = _d(request.equity)
    price = _d(request.price)
    stop_points = _d(request.stop_points)

    if not spec.complete or point_value is None or point_value <= 0:
        return _empty(request, NO_SPEC, ["the instrument terms cannot price one point"])
    if equity <= 0:
        return _empty(request, EQUITY_UNKNOWN, ["account equity is not known"])
    if stop_points <= 0:
        return _empty(request, NO_STOP, ["a signal without a stop cannot be sized"])
    if request.open_positions >= request.max_open_positions:
        return _empty(request, "max_open_positions", ["already at the open-position cap"])

    scale = _d(request.size_multiplier) * volatility_scale(
        volatility_points=request.volatility_points,
        target_volatility_points=request.target_volatility_points,
    )
    if scale <= 0:
        return _empty(request, NO_RISK_BUDGET, ["regime multiplier is zero"])
    if scale < 1:
        adjustments.append(f"size_scaled_to_{round(float(scale), 3)}")

    by_fraction = equity * _d(request.risk_fraction)
    budget = min(by_fraction, _d(request.max_loss_account)) * scale
    aggregate_room = equity * _d(request.max_aggregate_risk_fraction) - _d(request.open_risk_account)
    if aggregate_room <= 0:
        return _empty(
            request, AGGREGATE_RISK_CAP, ["open risk already uses the aggregate budget"]
        )
    if aggregate_room < budget:
        budget = aggregate_room
        adjustments.append(AGGREGATE_RISK_CAP)
    if budget <= 0:
        return _empty(request, NO_RISK_BUDGET, ["the permitted loss is zero"])

    loss_per_lot = stop_points * point_value
    if loss_per_lot <= 0:
        return _empty(request, NO_SPEC, ["one lot has no loss at this stop"])
    raw_lots = budget / loss_per_lot

    step = _d(request.volume_step if request.volume_step is not None else spec.volume_step)
    minimum = _d(request.volume_min if request.volume_min is not None else spec.volume_min)
    maximum = _d(request.volume_max if request.volume_max is not None else spec.volume_max)
    lots = _floor_to_step(raw_lots, step)
    lots = min(lots, maximum)

    # Leverage: notional uses the contract's own size, converted to account units.
    conversion = spec.conversion_rate if spec.conversion_rate is not None else Decimal(1)
    notional_per_lot = spec.contract_size * price * conversion
    leverage_room = _d(request.max_leverage) * equity - _d(request.open_notional_account)
    if leverage_room <= 0:
        return _empty(request, LEVERAGE_CAP, ["no leverage room left"])
    if lots * notional_per_lot > leverage_room:
        lots = _floor_to_step(leverage_room / notional_per_lot, step)
        adjustments.append(LEVERAGE_CAP)

    exposure = {str(key): _d(value) for key, value in request.currency_exposure.items()}
    # Share of the leverage allowance, matching the guardian: an equity-sized cap
    # would refuse the first ordinary lot rather than constrain concentration.
    cap = equity * _d(request.max_leverage) * _d(request.max_currency_exposure_fraction)
    base, quote = _pair_currencies(spec)
    for currency in (base, quote):
        if not currency:
            continue
        room = cap - exposure.get(currency, Decimal(0))
        if room <= 0:
            return _empty(
                request,
                CURRENCY_CAP,
                [f"{currency} exposure is already at the declared ceiling"],
            )
        if lots * notional_per_lot > room:
            lots = _floor_to_step(room / notional_per_lot, step)
            if CURRENCY_CAP not in adjustments:
                adjustments.append(CURRENCY_CAP)

    if lots < minimum or lots <= 0:
        return _empty(
            request,
            SIZE_BELOW_MINIMUM,
            [
                f"the risk budget buys {raw_lots:.4f} lots, below the broker minimum "
                f"{minimum}; rounding up would exceed the budget"
            ],
        )

    risk = lots * loss_per_lot
    notional = lots * notional_per_lot
    margin = notional * _d(spec.margin_rate)
    if margin > equity * Decimal("0.5"):
        return _empty(
            request,
            MARGIN_REFUSED,
            [f"margin {margin:.2f} exceeds half of equity {equity:.2f}"],
        )
    for currency in (base, quote):
        if currency:
            exposure[currency] = exposure.get(currency, Decimal(0)) + notional
    leverage_after = (_d(request.open_notional_account) + notional) / equity
    aggregate_after = _d(request.open_risk_account) + risk
    return Sizing(
        lots=lots,
        risk_account=risk,
        notional_account=notional,
        margin_account=margin,
        leverage_after=leverage_after,
        aggregate_risk_after=aggregate_after,
        currency_exposure_after=exposure,
        risk_budget=budget,
        loss_per_lot=loss_per_lot,
        volatility_scale=scale,
        refusals=(),
        adjustments=tuple(adjustments),
    )


def _empty(request: SizingInput, reason: str, detail: list[str]) -> Sizing:
    return Sizing(
        lots=Decimal(0),
        risk_account=Decimal(0),
        notional_account=Decimal(0),
        margin_account=Decimal(0),
        leverage_after=Decimal(0),
        aggregate_risk_after=_d(request.open_risk_account),
        currency_exposure_after={str(k): _d(v) for k, v in request.currency_exposure.items()},
        risk_budget=Decimal(0),
        loss_per_lot=Decimal(0),
        volatility_scale=Decimal("1"),
        refusals=(reason, *detail),
        adjustments=(),
    )


def _pair_currencies(spec: InstrumentSpec) -> tuple[str, str]:
    pair = str(spec.pair or "")
    if "/" in pair:
        base, _, quote = pair.partition("/")
        return base.strip().upper(), quote.strip().upper()
    return "", spec.quote_currency.upper() if spec.quote_currency else ""


def fractional_kelly(
    *,
    p_win: float,
    payoff_ratio: float,
    fraction: float = 0.25,
    cap: float = 0.01,
) -> dict[str, Any]:
    """Fractional Kelly, deliberately small and capped.

    ``f* = (p·b − (1−p)) / b`` for a bet paying ``b`` to 1.  The returned fraction is
    ``f* × fraction`` clamped to ``[0, cap]``.  It is **not** used by the desk by
    default: a Kelly fraction is only as good as the probability, and this build
    refuses to size from an uncalibrated one.
    """

    if payoff_ratio <= 0:
        return {"fraction": 0.0, "raw": 0.0, "usable": False, "reason": "payoff_not_positive"}
    raw = (p_win * payoff_ratio - (1.0 - p_win)) / payoff_ratio
    if raw <= 0:
        return {"fraction": 0.0, "raw": raw, "usable": False, "reason": "no_positive_edge"}
    value = min(cap, raw * fraction)
    return {
        "fraction": value,
        "raw": raw,
        "usable": True,
        "reason": "",
        "note": (
            "Kelly assumes the probability is right. This build does not size from it "
            "unless an out-of-sample calibration report supports the probability."
        ),
    }


__all__ = [
    "AGGREGATE_RISK_CAP",
    "CURRENCY_CAP",
    "EQUITY_UNKNOWN",
    "LEVERAGE_CAP",
    "MARGIN_REFUSED",
    "NO_RISK_BUDGET",
    "NO_SPEC",
    "NO_STOP",
    "SIZE_BELOW_MINIMUM",
    "Sizing",
    "SizingInput",
    "fractional_kelly",
    "size_position",
    "volatility_scale",
]
