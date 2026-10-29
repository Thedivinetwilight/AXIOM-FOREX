"""Broker/data-provider decisions that remain open.

These are documentation objects only.  They do not read configuration or
select a vendor.  The offline conformance layer in `axiom_forex/conformance/`
now records a machine-readable list of required provider-specific values and
validates deterministic fake adapters.  A future real integration must supply
the actual values and pass provider-specific tests before it is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OpenDecision:
    key: str
    depends_on: str
    provider_neutral_stance: str
    conformance_required: str


OPEN_DECISIONS: tuple[OpenDecision, ...] = (
    OpenDecision(
        "broker_identity",
        "pair list, aliases, precision, contract size, margin, financing, order types, reconciliation",
        "BrokerAdapter protocol plus BrokerCapabilities; local paper adapter and an opt-in MT5 live adapter (axiom_forex/brokers/mt5)",
        "offline fake-MetaTrader5 tests for symbol mapping, retcodes, fills, and recovery; the broker's own terms still need the operator's account",
    ),
    OpenDecision(
        "data_provider_identity",
        "bid/ask tick or bar granularity, timestamps, corrections, history, licensing",
        "MarketDataProvider protocol plus normalized ForexQuote/ForexQuoteBar; the MT5 adapter supplies bid/ask ticks and tick-built bars",
        "DST, holiday, stale/corrected quote, and historical completeness tests; MT5 tick depth depends on the terminal",
    ),
    OpenDecision(
        "account_currency_and_rates",
        "pip value, notional, margin, PnL, daily loss",
        "explicit AccountProfile and ConversionRateBook; the MT5 adapter reads the account currency from the terminal and refuses a mismatch",
        "rate source, timestamp, and staleness conformance; the account currency itself is confirmed only on the operator's terminal",
    ),
    OpenDecision(
        "leverage_and_margin",
        "position size, margin calls, liquidation behavior",
        "explicit leverage and per-instrument margin multiplier; MT5 margin is taken from order_calc_margin and the declared leverage must match the account",
        "broker margin schedule and negative-equity policy verification on the operator's account",
    ),
    OpenDecision(
        "trading_calendar",
        "24/5 operation, daily breaks, holidays, DST, weekend gaps, rollover cut",
        "versioned explicit UTC windows scoped to instruments; generic 24/5 is fixture-only, and the MT5 adapter reports broker-server time rather than assuming a session",
        "broker calendar replay around DST and holidays, using the operator's broker sessions",
    ),
    OpenDecision(
        "costs_and_rollover",
        "commission, spread, slippage, financing, triple rollover",
        "explicit versioned CostModel and RolloverPolicy; no assumed Wednesday; MT5 swap/commission are read per deal, never inferred",
        "broker rate-table and triple-rollover-day verification against the operator's contract specifications",
    ),
    OpenDecision(
        "strategy_and_correlation",
        "production strategy, correlated pairs, exposure groups",
        "strategy registry/lifecycle and explicit correlation matrix; no active strategy",
        "out-of-sample, walk-forward, and correlation-stability tests",
    ),
    OpenDecision(
        "offline_conformance_contract",
        "all market-data and broker capability terms listed in docs/PROVIDER_CONFORMANCE.md",
        "versioned contracts, deterministic fake adapters, and fail-closed checks; the MT5 adapter passes the offline suite with a fake MetaTrader5 module",
        "provider-specific adapter tests run offline now; items that need the broker account are listed in axiom_forex/brokers/mt5/conformance.py",
    ),
    OpenDecision(
        "provider_profile_onboarding",
        "official provider documentation/source, effective dates or versions, and demo/production classification",
        "versioned JSON profile with provenance for every required value; synthetic fixture active only",
        "strict validation, compatibility checks, and safe operator report before any real adapter review",
    ),
    OpenDecision(
        "historical_data_import",
        "vendor export mapping, receive-time/quote-id availability, restatement policy, historical depth",
        "provider-neutral canonical CSV with explicit declarations, hashes, provenance, and quarantine",
        "a reviewed vendor export adapter and provider-specific documentation terms after selection",
    ),
    OpenDecision(
        "multi_pair_research_evaluation",
        "per-pair calendar/holiday/DST terms, conversion rates for every currency leg, "
        "correlation matrix, aggregate exposure limits, cost and swap terms, minimum "
        "history depth and accepted coverage ratio",
        "provider-neutral research evaluation over imported datasets with explicit terms "
        "injected; qualification never activates a strategy",
        "provider-specific calendar, rate, correlation, and cost terms after selection, "
        "plus a reviewed vendor export adapter",
    ),
    OpenDecision(
        "stress_and_sensitivity_inputs",
        "realistic spread/slippage/commission/swap sizes, quote-delay and "
        "missing-quote tolerance, conversion-rate volatility, correlation "
        "regimes, and the provider terms that would justify them",
        "explicitly configured stress scenarios with an evidence class; "
        "hypothetical unless imported or documented with a reference, and "
        "never qualifying or activating a strategy",
        "provider-backed stress parameters with documented references, plus "
        "the history needed to derive observed distributions after selection",
    ),
)


def decision_index() -> dict[str, OpenDecision]:
    return {decision.key: decision for decision in OPEN_DECISIONS}
