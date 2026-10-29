# Provider and broker conformance contract

## Status and boundary

This milestone adds an **offline, provider-neutral conformance layer**. It does
not choose a broker, data provider, account currency, leverage, or strategy. It
does not add network access, credentials, external account access, live order
placement, deployment, or runtime-setting changes. `ExternalBrokerBoundary`
remains disabled and `LocalPaperBroker` remains the only active broker.

The conformance layer validates that a future integration can state its terms
explicitly and replay realistic forex behavior against deterministic fixtures.
A passing report means the local contract is internally consistent and its
fail-closed behavior was observed; it is **not** a claim of profitability,
broker compatibility, or live-trading readiness.

## Components

- `axiom_forex/conformance/contracts.py`: versioned market-data and broker
  capability dataclasses, findings/checks/report types, and the machine-readable
  `REQUIRED_PROVIDER_VALUES` list.
- `axiom_forex/conformance/adapters.py`: deterministic read-only market-data
  adapter and local broker order-lifecycle adapter. Neither contacts a provider
  nor places an external order.
- `axiom_forex/conformance/fixtures.py`: builds explicit local fixtures from M1
  values or from an existing platform's explicit values. No environment
  variables, credentials, account state, or network responses are read.
- `axiom_forex/conformance/checks.py`: fail-closed checks for contract
  consistency, payload normalization, calendar behavior, freshness, costs,
  conversion, order lifecycle, and restart recovery.
- `axiom_forex/conformance/suite.py`: `ConformanceSuite.for_platform(...).run()`
  entry point.
- `tests/test_forex_conformance.py`: focused contract, fake-adapter, workflow,
  persistence, restart, API/desktop/dashboard/report, and CLI tests.

An offline [historical data import](HISTORICAL_DATA_IMPORT.md) layer consumes
user-supplied canonical CSV data only after these contracts and the profile
terms accept it: pair, provider alias, timeframe, quote/bar semantics, calendar
identity, history bounds, spread limit, and data class are all compatibility
checks. Imported data is research/backtest material and never reaches a paper or
external order path.

Provider profile onboarding is the next layer above these contracts. It uses
the 37 keys below as the source of truth for a versioned JSON profile with
provenance and effective dates. See [Provider profile onboarding](PROVIDER_PROFILE_ONBOARDING.md)
for the schema, the empty template, completion instructions, and activation
rules.

## What the offline fixtures replay

The deterministic fixtures cover:

- separate executable bid and ask prices with an explicit spread;
- stale quotes, crossed quotes, incomplete bars, and missing bars without
  interpolation;
- explicit quote corrections by quote ID at the same timestamp;
- half-open `[start, end)` historical bar requests and read-only provider
  behavior;
- an `America/New_York` DST transition from UTC-05:00 to UTC-04:00;
- a holiday, a 17:00-18:00 local daily break, a weekend gap, calendar validity
  bounds, and an unavailable instrument;
- rejected orders, partial fills, duplicate incremental fills, confirmed
  cancellation, unknown order outcomes, reconciliation, and idempotency
  conflicts;
- restart recovery that preserves unresolved orders and blocks resubmission.

The workflow fixture explicitly sets `triple_rollover_weekday=None` (no
triple-rollover rule assumed). The standalone conformance environment uses an
explicit local weekday value only to verify that the contract can represent a
triple-rollover day. Neither value is a production assumption.

## Fail-closed behavior

`ConformanceReport.passed` is true only when every check is informational or
successful. Any error finding fails the report. The offline paper workflow calls
`ForexPlatform.refresh_conformance_report()` after normalized data is loaded and
blocks before qualification, routing, or order execution when the report fails.
The result, health, status, workflow snapshot, and a durable `conformance`
snapshot record the outcome.

Health includes `provider_broker_conformance` and becomes `UNSAFE` on failure.
`verify()` includes `provider_broker_conformance` as one of its checks. The
read-only API exposes `/conformance`; `/status`, `/workflow`, `/desktop`,
`/report`, and `/dashboard` include or summarize the same report.

A separate provider-profile onboarding report is checked before the conformance
report in the paper workflow. Missing, invalid, stale, contradictory, or
incompatible profile terms block activation before qualification, routing, or
order execution. Health includes `provider_profile_onboarding`, `verify()`
includes it as a check, and `/profile` plus the other operator surfaces expose
the same safe report. Demo and production profiles can be validated but cannot
activate external execution.

Unresolved orders remain unresolved after restart. The fake broker's
`NO_RESUBMIT_UNTIL_RECONCILED` policy and the workflow's
`unresolved_order_present` block both prevent new order submission.

## Required provider-specific values

Once a broker and data provider are chosen, supply every value below through
explicit, versioned contracts and recorded/sanitized fixtures or a separately
reviewed integration adapter. The keys are the exact machine-readable keys in
`REQUIRED_PROVIDER_VALUES`; the report's `required_values` array contains the
actual supplied values and `supplied` flags. Fixture examples are labeled local
examples, **not** production defaults.

| Area | Key | What must be supplied | Labeled fixture example |
| --- | --- | --- | --- |
| market | `market.provider_id` | Canonical data-provider identity used for alias resolution. | `SYNTHETIC_FEED` |
| market | `market.capability_version` | Version of the market-data capability declaration. | `market-conformance-v1` |
| market | `market.source` | Provenance of recorded quotes and bars. | `SYNTHETIC_CONFORMANCE_FEED` |
| market | `market.supported_pairs` | Canonical currency pairs the adapter is allowed to publish. | `AAA/BBB` |
| market | `market.provider_symbols` | Canonical pair to provider symbol mapping. | `AAA_BBB` |
| market | `market.supported_timeframes` | Completed-bar timeframes with defined boundaries. | `1m` |
| market | `market.quote_timestamp_basis` | Meaning and timezone basis of quote timestamps. | `UTC_SOURCE_TIMESTAMP` |
| market | `market.clock_source` | Clock source and synchronization assumption. | `SYNTHETIC_UTC_CLOCK` |
| market | `market.bid_ask_semantics` | Whether bid/ask are separate executable prices. | `SEPARATE_BID_ASK` |
| market | `market.quote_id_required` | Whether every quote has a stable provider identifier. | `true` |
| market | `market.received_at_required` | Whether local receive time is mandatory for freshness. | `true` |
| market | `market.quote_correction_policy` | How corrected quotes replace earlier quotes. | `EXPLICIT_REPLACEMENT_BY_QUOTE_ID` |
| market | `market.bar_timestamp_basis` | Whether bar timestamps are start, end, or another documented instant. | `UTC_BAR_START` |
| market | `market.bar_completion_rule` | Rule that guarantees only completed bars are returned. | `COMPLETE_ONLY` |
| market | `market.bar_ohlc_semantics` | Bid/ask OHLC aggregation semantics and volume definition. | `SEPARATE_BID_ASK_OHLC` |
| market | `market.gap_policy` | How session gaps and missing bars are represented. | `REPORT_NO_INTERPOLATION` |
| market | `market.history_bounds` | Explicit history coverage validity bounds in UTC. | `2026-03-06..2026-03-13 UTC` |
| market | `market.freshness` | Quote age, future skew, and feed latency tolerances. | `5s / 1s / 1s` |
| market | `market.calendar` | Broker calendar identity, version, timezone, windows, holidays, breaks, DST, validity. | `synthetic-24x5-v1 / calendar-v1 / America/New_York` |
| market | `market.instrument_spec` | Pip, tick, precision, contract size, min/step/max quantity, status, spec version. | `spec-v1` |
| market | `market.max_spread_pips` | Maximum executable spread accepted by the gate. | `5 pips` |
| broker | `broker.broker_id` | Canonical broker identity used in audit records. | `SYNTHETIC_CONFORMANCE_BROKER` |
| broker | `broker.capability_version` | Version of the broker capability declaration. | `broker-conformance-v1` |
| broker | `broker.execution_mode` | Execution mode permitted by this repository. | `LOCAL_PAPER` |
| broker | `broker.external_orders_supported` | Must remain false for this offline milestone. | `false` |
| broker | `broker.order_types` | Supported order types and simulated-intent capability. | `SIMULATED_INTENT, MARKET, LIMIT` |
| broker | `broker.partial_fills` | Whether partial fills and incremental fill quantities are supported. | `true` |
| broker | `broker.cancellation` | Cancellation semantics and confirmation requirements. | `CONFIRMED_TRANSITION` |
| broker | `broker.reconciliation` | Order-history source, status map, retention, and unknown-outcome policy. | `SYNTHETIC_CONFORMANCE_STATUS_MAP / LOCAL_FIXTURE_SNAPSHOT` |
| broker | `broker.idempotency` | Client-order idempotency key and broker order identity source. | `CLIENT_ORDER_ID / LOCAL_DETERMINISTIC_BROKER_ORDER_ID` |
| broker | `broker.rejection_codes` | Provider rejection/error codes that must be mapped deterministically. | `BROKER_REJECTED, UNKNOWN_OUTCOME, IDEMPOTENCY_CONFLICT, CANCEL_CONFIRMED` |
| broker | `broker.sizing` | Pip, tick, precision, contract size, min/step/max quantity. | `spec-v1 / LOTS / 0.01 step` |
| broker | `broker.trading_hours` | Calendar identity, timezone, DST, holidays, daily breaks, weekend gaps, validity. | `synthetic-24x5-v1 / calendar-v1 / America/New_York` |
| broker | `broker.costs` | Commission, spread/slippage, financing, swaps, triple-rollover day, cutoff, source versions. | `cost-v1 / rollover-policy-v1 / swap-fixture-v1` |
| broker | `broker.account_currency` | Account currency and timestamped conversion-rate source/pairs/freshness. | `CCC / SYNTHETIC_FX_SOURCE / BBB-CCC, AAA-CCC` |
| broker | `broker.margin` | Leverage, max leverage, margin multiplier, margin-call and negative-equity policy. | `leverage 5 / max 10 / multiplier 1` |
| broker | `broker.recovery` | Durable order/fill/position snapshot and unresolved-order no-resubmit behavior. | `SQLite snapshots / NO_RESUBMIT_UNTIL_RECONCILED` |

Credentials, access tokens, account identifiers that look like credentials,
private keys, and live endpoints are deliberately absent from this list and
from the repository. They must remain outside this repository and be handled by
an independently reviewed integration boundary; they are not required to pass
the offline conformance suite.

## Decisions to record after choosing a broker/provider

1. **Identity and scope:** provider ID, broker ID, capability versions,
   supported canonical pairs, provider aliases, and supported timeframes.
2. **Market data semantics:** quote timestamp basis, clock source, bid/ask
   semantics, source/receive timestamps, correction policy, bar timestamp
   basis, completion rule, OHLC semantics, gap policy, history bounds, and
   freshness tolerances.
3. **Instrument terms:** pip, tick, quote precision, contract size, quantity
   unit, min/step/max quantity, status, spec version, and source.
4. **Calendar:** timezone, DST-aware UTC session windows, holidays, daily
   breaks, weekend gaps, validity bounds, instrument availability, and the
   broker's actual daily rollover cutoff.
5. **Costs and financing:** commission, slippage, spread limits, financing,
   swap rate cards and versions, triple-rollover weekday or an explicit `None`,
   and source versions.
6. **Account and conversion:** account currency, leverage, max leverage,
   margin multiplier, negative-equity policy, rate source/version, rate pairs,
   timestamps, and freshness policy.
7. **Orders and recovery:** order types, rejection/status mappings,
   idempotency key, broker order identity, partial-fill semantics, cancellation
   confirmation, reconciliation source/retention, unknown-outcome policy, and
   durable snapshot behavior.

Do not infer these values from equity behavior, a generic 24/5 assumption, or
historical simulation. Missing, stale, inconsistent, or unresolved values must
fail closed.

## Verification commands

```bash
/tmp/axiom-venv/bin/python -m pytest -q tests/test_forex_conformance.py
/tmp/axiom-venv/bin/python -m pytest -q tests/test_forex_provider_profiles.py
/tmp/axiom-venv/bin/python -m pytest -q
python3 -m axiom_forex conformance
python3 -m axiom_forex profile
python3 -m axiom_forex verify
python3 -m axiom_forex workflow --workflow-id conformance-smoke
```

All commands above are repository-local and offline. They do not contact a
provider, read credentials, change runtime settings, deploy code, or affect the
running live system.

## Remaining limitations

- No real broker or data-provider adapter is implemented or enabled. Provider
  profiles are onboarding/validation artifacts, not runtime adapters.
- No production strategy, account currency, leverage, correlation matrix, or
  calendar is assumed.
- Provider quota, throttling, licensing, transport security, and credential
  handling need independent review before any external integration.
- Passing conformance checks validates offline contracts and fixture replay,
  not broker certification, historical profitability, or live readiness.
