"""Fixture builders for the provider-backed paper lane.

Everything here is *synthetic test data*: a fake MetaTrader 5 module for the
terminal surface, a made-up account, made-up costs, and a made-up calendar.  No
number in this module describes any real broker, and nothing here is evidence
about a real market.

The point is to exercise the *wiring*: an operator setup document, a
catalogue-resolved symbol, the browser's paper stack, the runner, and the live
leg -- with the real code paths and the real refusals.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.data.models import ForexQuoteBar, QuoteSideBar

from axiom_forex.config.provider_setup import ProviderSetup, load_provider_setup

PAIR = "EUR/USD"
BROKER_SYMBOL = "EURUSD.pro"
PIP = Decimal("0.0001")
UTC = timezone.utc

#: 2026-03-10 13:50 UTC: inside the synthetic setup calendar below, and late
#: enough that the declared conversion basis is not in the future.
START = datetime(2026, 3, 10, 13, 50, tzinfo=UTC)

PROMOTING_EVIDENCE = {
    "evaluated": True,
    "data_class": "USER_SUPPLIED_HISTORICAL",
    "passed": True,
    "failed_criteria": [],
    "dataset_id": "synthetic-provider-test-dataset",
    "criteria_version": "paper-runner-criteria-v1",
}


def setup_payload(**overrides: Any) -> dict[str, Any]:
    """A complete, synthetic ``axiom-provider-setup-v1`` document."""

    payload: dict[str, Any] = {
        "schema": "axiom-provider-setup-v1",
        "account": {
            "account_id": "synthetic-provider-account",
            "currency": "USD",
            "leverage": "100",
            "initial_capital": "10000",
            "conversion_source": "OPERATOR_FIXED_BASIS",
            "entry_exposure_limit": {
                "amount": "20000",
                "currency": "USD",
                "version": "entry-limit-v1",
                "source": "SYNTHETIC_TEST_POLICY",
            },
        },
        "costs": {
            "model_id": "synthetic-costs",
            "version": "v1",
            "commission_rate": "0.00002",
            "slippage_pips": "0.2",
            "financing_rate_daily": "0",
            "source": "SYNTHETIC_TEST_TERMS",
        },
        "rollover": {
            "schedule_id": "synthetic-rollover",
            "version": "v1",
            "daily_cutoff_utc": "21:00",
            "triple_rollover_weekday": 2,
            "source": "SYNTHETIC_TEST_TERMS",
        },
        "swap": [
            {
                "pair": PAIR,
                "long_rate": "-0.0000074",
                "short_rate": "0.0000012",
                "source": "SYNTHETIC_TEST_TERMS",
                "version": "v1",
            }
        ],
        "risk": {
            "max_order_notional_account": "20000",
            "max_gross_exposure_account": "20000",
            "max_margin_account": "20000",
            "max_abs_currency_exposure_account": "20000",
            "max_correlation_group_exposure_account": "20000",
            "max_open_positions": 1,
            "max_daily_loss_account": "200",
            "max_trades_per_day": 5,
            "max_spread_pips": "2",
            "max_leverage": "100",
            "limits_version": "synthetic-risk-v1",
        },
        "calendar": {
            "calendar_id": "synthetic-provider-calendar",
            "version": "v1",
            "source": "SYNTHETIC_TEST_CALENDAR",
            "timezone_name": "UTC",
            "valid_from": (START - timedelta(days=1)).isoformat(),
            "valid_until": (START + timedelta(days=2)).isoformat(),
            "windows": {
                PAIR: [
                    [
                        (START - timedelta(hours=2)).isoformat(),
                        (START + timedelta(hours=6)).isoformat(),
                        "synthetic-session",
                    ]
                ]
            },
            "holidays": {PAIR: []},
        },
        "symbols": [
            {
                "pair": PAIR,
                "broker_symbol": BROKER_SYMBOL,
                "suffix": ".pro",
                "pip_size": "0.0001",
                "correlation_group": "USD_MAJORS",
                "margin_multiplier": "1",
                "spec_version": "operator-spec-v1",
            }
        ],
        "conversion_rates": [
            {
                "from": "EUR",
                "to": "USD",
                "rate": "1.1000",
                "timestamp": (START - timedelta(minutes=5)).isoformat(),
            }
        ],
        "execution": {"quantity": "0.10", "max_open_positions": 1, "allow_short": True},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key] = {**payload[key], **value}
        else:
            payload[key] = value
    return payload


def write_setup(directory: Path, **overrides: Any) -> ProviderSetup:
    path = Path(directory) / "provider_setup.json"
    path.write_text(json.dumps(setup_payload(**overrides)), encoding="utf-8")
    return load_provider_setup(path)


def symbol_request(setup: ProviderSetup, pair: str = PAIR):
    from axiom_forex.brokers.mt5.catalogue import SymbolRequest

    terms = setup.terms_for(pair)
    assert terms is not None, "the setup must carry symbol terms"
    return SymbolRequest(
        pair=terms.pair,
        pip_size=terms.pip_size,
        calendar_id=terms.calendar_id,
        calendar_version=terms.calendar_version,
        correlation_group=terms.correlation_group,
        margin_multiplier=terms.margin_multiplier,
        spec_version=terms.spec_version,
        broker_symbol=terms.broker_symbol,
        suffix=terms.suffix,
        source=terms.source,
    )


def make_bar(mid: Decimal, timestamp: datetime, spec: Any) -> ForexQuoteBar:
    half = PIP / Decimal(2)
    bid = mid - half
    ask = mid + half
    bar = ForexQuoteBar(
        pair=str(spec.canonical_pair or spec.pair),
        timeframe="1m",
        timestamp=timestamp,
        duration_seconds=60,
        bid=QuoteSideBar(open=bid, high=bid + half, low=bid - half, close=bid),
        ask=QuoteSideBar(open=ask, high=ask + half, low=ask - half, close=ask),
        source="SYNTHETIC_PROVIDER_TEST_BARS",
        quote_count=12,
        last_quote_at=timestamp + timedelta(seconds=59),
        complete=True,
    )
    validation = bar.validate(spec)
    assert validation.valid, validation.reason
    return bar


def mid_series(n_up: int = 25, n_down: int = 20) -> list[Decimal]:
    values: list[Decimal] = []
    value = Decimal("1.1000")
    for _ in range(n_up):
        values.append(value)
        value += PIP
    for _ in range(n_down):
        value -= PIP
        values.append(value)
    return values


def make_bars(mids: list[Decimal], spec: Any, *, start: datetime = START) -> list[ForexQuoteBar]:
    return [
        make_bar(mid, start + timedelta(minutes=index), spec)
        for index, mid in enumerate(mids)
    ]


class Mode:
    """Read-only stand-in for the execution-mode controller.

    ``session_account`` is the account signature the operator confirmed when LIVE
    was selected; the live workflow refuses orders when the terminal is on a
    different account, so the stand-in carries it like the real controller.
    """

    def __init__(
        self,
        mode: str = "PAPER",
        live_effective: bool = False,
        session_account: str = "",
    ) -> None:
        self.mode = mode
        self.live_effective = live_effective
        self.session_account = session_account


def candidate_for(evidence: dict | None = None, *, fast: int = 8, slow: int = 21):
    from dataclasses import replace

    from axiom_forex.paper_runner.strategy import candidate_family

    base = next(
        item
        for item in candidate_family()
        if item.parameters == {"fast_period": fast, "slow_period": slow}
    )
    return replace(base, evidence=dict(evidence or PROMOTING_EVIDENCE))
