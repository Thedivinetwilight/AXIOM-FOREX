# AXIOM-FOREX — Architecture Assessment and Implementation Plan

**Status:** 2026-10-27 · research and paper trading only · live trading disabled.

This is an adaptation plan, not a profitability claim or a live-trading readiness statement. No broker, data provider, account currency, leverage, or production strategy has been selected.

## 1. What was inspected

- **Target repository:** `Thedivinetwilight/AXIOM-FOREX`, branch `arena/f67c2859-axiom-forex`. At inspection time it contained only a two-line `README.md`: no application code, configuration, database schema, or tests.
- **Source architecture:** the user-owned [`Thedivinetwilight/AXIOM`](https://github.com/Thedivinetwilight/AXIOM/tree/0314db2c2d1473b0b63e04ce7c1455695f93a885) repository, commit `0314db2c2d1473b0b63e04ce7c1455695f93a885`, whose README identifies it as **Arena 3.0.5**.
- **Source size:** 200 Python files, about 17,888 lines under `app/`, 39 test files, about 9,176 test lines. Python 3.11+; FastAPI/Pydantic for the API, standard library for the research core, SQLite for persistence, PySide6 for the optional desktop UI.
- **Observed test baseline:** in an isolated Python 3.11 virtual environment, `pytest --collect-only` collected **546 tests**; the full run produced **545 passed, 1 skipped**. The skip was an external public instrument-file test that could not reach its URL. The source's `ARENA_FINAL_VERIFICATION.md` records a historical **491-test** release baseline, so the current checkout — not that document — is the test-count authority for this migration.
- No source credentials or working accounts were used. External-network behavior was not required to inspect the architecture.

The source core is modular and mostly market-neutral in shape, but its real-data adapters, defaults, instrument master, sandbox broker, sample calendar, and accounting semantics are centered on Indian equities (Upstox/NSE). A public Binance historical adapter exists but does not change that core.

## 2. Architecture to preserve

| AXIOM layer | Representative source modules | Forex adaptation |
|---|---|---|
| Configuration and safety | `app/config/settings.py`, `tools/security_scan.py` | Keep immutable paper-only settings and validation; remove venue-specific defaults and add explicit provider/account/calendar/cost profiles. |
| Data normalization and providers | `app/data/models.py`, `provider.py`, `validator.py`, `candles.py`, `market_events.py`, `realtime.py`, `historical.py`, `upstox_stream.py` | Replace single-price OHLCV as the executable source with timestamped bid/ask quotes and complete bid/ask bars. Preserve UTC normalization, completed-bar semantics, cache/validator separation, and provider-neutral interfaces. Do not port Upstox decoding as an assumed provider. |
| Instrument universe and history | `app/universe/`, `app/history/`, `app/research/datasets.py` | Replace ISIN/exchange/option/lot-size records with versioned currency-pair specifications and explicit provider aliases. Preserve sync diffs, dataset hashes, coverage, and quality events, but make gap detection calendar-aware. |
| Indicators, strategies, regimes | `app/indicators/`, `app/strategies/`, `app/learning/regime*.py`, `app/market_events/` | Keep pure, prefix-only indicator/strategy interfaces. Use an explicitly derived mid only for research; never treat it as executable. Make volume-based and time-of-day features optional and session-aware. |
| Validation, backtesting, research | `app/backtesting/`, `app/validation/`, `app/tournament/`, `app/research/` | Preserve chronological splits, one-use holdout, walk-forward, robustness, next-bar execution, and no-look-ahead rules. Add bid/ask fills, gaps, financing, currency conversion, and out-of-sample cost stress. |
| Strategy lifecycle and learning | `app/learning/`, `app/engine/adaptive.py` | Preserve the RESEARCH → VALIDATION → WALK_FORWARD → SHADOW → CANARY → APPROVED ladder, positive-OOS gating, degradation, and the rule that learning cannot change hard risk or mode. Version dataset, calendar, instrument-spec, and cost-model identities with each candidate. |
| Portfolio and hard risk | `app/backtesting/portfolio.py`, `app/risk/limits.py`, `app/risk/net_edge.py`, `app/engine/state.py` | Keep hard limits outside learning. Replace single-currency long-only inventory with signed base-currency positions, pip value in account currency, margin, conversion rates, aggregate currency exposure, and correlated-pair limits. Exits stay possible under a separately controlled risk-reduction path. |
| Execution and broker adapters | `app/paper_trading/`, `app/brokers/sandbox.py`, `app/engine/sandbox_engine.py` | Keep `BrokerInterface`, durable intents, idempotency, reconciliation, emergency STOP, and unresolved-order handling. Expose only a local paper adapter initially. A provider-neutral `BrokerCapabilities` object describes precision, order types, fills, and calendars once a broker is selected. |
| Database, audit, recovery | `app/database/`, `app/logging/` | Preserve additive migrations, atomic checkpoints, one-writer locking, idempotent rows, backups, and secret-safe logs. Add pair/base/quote sides, bid/ask, quote time, spread, spec/calendar/cost versions, financing, margin, and conversion-rate provenance. |
| Monitoring and reporting | `app/monitoring/`, `app/reporting/` | Preserve reality-gap, anomaly, system-health, and honest report labeling. Add quote freshness, session/calendar health, weekend gaps, rollover events, margin, and currency-exposure monitoring. |
| API, dashboard, desktop, CLI | `app/api/`, `desktop/`, `run.py`, `app/__main__.py`, `scripts/` | Preserve bearer auth, read-only presentation, API/UI separation, and paper/sandbox mode badges. Add calendar status, quote age, spread, margin/exposure, and clear “LIVE DISABLED” state. The UI must not bypass risk. |
| Tests | `tests/` | Preserve the source's focus on successful paths, safe failures, restart/replay parity, authentication, causality, and static bypass scans. Add the forex-specific cases in §7. |

No wholesale copy of the Upstox-specific modules should become the default runtime. Provider-neutral ports should be extracted first; venue adapters are later, opt-in conformance work.

## 3. Safety gates and behavior to preserve

1. **Mode boundary.** `MODE=PAPER`, `LIVE_TRADING=false`, an explicit paper-only broker allowlist, and a non-instantiable real-broker marker. Settings reject `BROKER=LIVE`; API/health surfaces report live as disabled.
2. **Credential boundary.** Secrets come only from ignored local environment; no credential, account identifier, order URL, or live endpoint is committed. Tokens are excluded from reprs and logs. Any future adapter must be sandbox/demo only and separately reviewed.
3. **Data boundary.** Timezone-aware UTC timestamps, positive finite prices, strict ordering/duplicate/OHLC checks, completed candles only, no interpolation or fabricated bars, and explicit calendar-aware gap reporting.
4. **Market-status boundary.** Connection is not market-open status. The engine checks market status, feed staleness, and data gaps before signals. AXIOM-FOREX must additionally check instrument availability, calendar version/expiry, daily breaks, holidays, DST, and weekend gaps.
5. **Execution boundary.** Orders are copied at submission, duplicate/backdated orders are rejected, quantities and prices are validated, fills are recorded, and bars outside a session cannot execute. Deterministic intent IDs and a durable state machine prevent blind resubmission. `UNKNOWN`, `REMOTE_LOOKUP`, and `UNRESOLVED` orders never become new orders.
6. **Hard-risk boundary.** Frozen limits govern position value, open positions, daily loss, and trades per day; they live outside learning. Net-edge, quality, liquidity, volatility, entry-timing, and alpha-decay gates are additive and must not override hard risk. A persisted emergency STOP and execution pause survive restarts.
7. **Strategy lifecycle boundary.** Only fully approved, active, positive-OOS candidates are tradable. No qualified candidate means no entry; in-sample return cannot authorize trading.
8. **Database/audit boundary.** Atomic paper checkpoints, one writer, idempotent order/fill/transition records, recovery from recorded fills, online backups, and bounded audit history.
9. **API boundary.** Protected routes require a private API token; extra/unknown fields and path traversal are rejected; UI is presentation-only except explicit STOP/RESUME controls.

### Safety gap to correct, not reproduce

The 3.0.5 engine documents its added gates as fail closed, but several `try/except` blocks in `app/engine/sandbox_engine.py` continue after a gate error, invent bid/ask from a candle close when quote sides are missing, and substitute fixed values for spread, liquidity, expected edge, and confidence. That behavior conflicts with the required forex safety rule. AXIOM-FOREX should preserve the **intent** of the gate chain but make every missing/invalid input produce an explicit rejection event and block new orders. Risk-reducing exits may use a separate, idempotent reconciliation path; they must never rely on fabricated executable prices.

## 4. Equity assumptions and forex replacements

| Existing assumption | Evidence | Forex replacement |
|---|---|---|
| A single OHLC price plus volume is the market record. | `Candle`, `CSVProvider`, `LiveCandleBuilder`, `Portfolio` | `ForexQuote` (timestamped bid/ask, source, sequence, received time) and `ForexQuoteBar` (separate bid/ask OHLC, completeness, quote count). Derived mid is research-only; BUY uses ask, SELL uses bid. |
| Bid/ask fields exist but are not used to build bars or execute. | `MarketEvent` has `bid`/`ask`; `MarketEventApplier` uses `ltp`. | Provider normalization must carry both sides through bars, validation, backtests, and the pre-trade gate. Missing one side is a rejection. |
| Exchange volume is available and supports participation limits. | `MAX_PARTICIPATION`, `Candle.volume`, volume-based regimes | Spot FX has no universal consolidated volume. Treat broker tick volume as an explicitly labeled proxy, or reject volume-dependent checks when unavailable. Never invent depth. |
| Long-only, no shorts, no leverage, no margin; fractional quantities. | README §6, `Portfolio`, `PaperBroker` | Signed positions in base-currency units, explicit contract size, minimum/step/maximum trade size, leverage/margin from account terms, and deterministic quantity rounding. |
| `quantity × price` is account-currency notional; one currency for cash and marks. | `OrderIntent.notional`, `Portfolio`, default `currency="INR"` | Convert quote-currency P&L and pip value into the account currency using timestamped conversion rates. Missing/stale/non-finite conversion data must fail closed. |
| Commission and spread are proportional; no rollover or financing. | `ExecutionConfig`, source known limitations | Explicit `CostModel` and `RolloverSchedule` interfaces: commission per notional/lot, slippage, bid/ask spread, swap/financing, triple rollover where broker-defined, and currency conversion. Each model is versioned. |
| `pip`, lot, precision, and tick size are absent or equity-specific. | `ISIN`, `lot_size`, `tick_size`, fractional sizing | `CurrencyPairSpec`: canonical `BASE/QUOTE`, base/quote, pip size, quote precision, price tick, contract size, min/step size, provider aliases, margin terms, availability, and spec version. |
| Continuous calendar or NSE 09:15–15:30 Asia/Kolkata; no holidays by default. | `ContinuousCalendar`, `SessionCalendar`, `market_timezone`, `annualization=252` | `TradingCalendar` with IANA timezone, explicit session windows, daily breaks, holidays, DST-aware UTC instants, weekend gaps, instrument-specific availability, version, validity interval, and source. Unknown or expired calendar means closed. A synthetic 24/5 fixture is for tests only, never a production default. |
| Trading day and performance use UTC/Asia-Kolkata conventions. | `DailyRiskAccount`, `metrics.py` | Trading-day boundary comes from the selected broker calendar. Metrics use observed trading days and an explicit annualization convention; weekend gaps are not treated as zero-return observations by default. |
| Instrument identity is an Upstox key/ISIN and equity product code. | `universe/manager.py`, `brokers/sandbox.py`, settings | Canonical pair identity plus provider alias, with strict alias resolution and no fallback to another instrument. Unknown, ambiguous, delisted, or unavailable instruments reject. |
| Order quantity is an integer share count in the engine. | `_size_entry`, sandbox tests | Trade size is base units or lots according to an explicit spec; enforce min/step/max and reject unsafe rounding. |
| Provider endpoints, historical chunk rules, OAuth, and order history are Upstox-specific. | `history/manager.py`, `upstox_*`, settings | Read-only provider interfaces plus local fixtures. Concrete REST/WSS adapters are deferred until provider selection. |
| Strategy/regime features assume volume and a local equity session shape. | `learning/regime_engine.py`, event triggers | Session-aware feature windows; volume features optional and explicitly labeled; no look-ahead across closures or DST transitions. |
| Default account currency, calendar, symbol, and annualization are preconfigured. | `Settings` defaults | No production default for account currency, leverage, pair universe, calendar, cost model, or strategy. Missing configuration blocks new orders. |

## 5. Open decisions and provider-neutral stance

| Decision | What depends on it | Stance until selected |
|---|---|---|
| Broker / account type / jurisdiction | Pair list and aliases, precision, contract size, min/step size, leverage, margin calls, commissions, financing/triple-rollover day, trading-day cut, holidays, DST/local breaks, order types, partial fills, reconciliation, demo API limits | Define `BrokerCapabilities` and `BrokerAdapter` interfaces; ship only a local paper adapter. No live or real-money adapter, credentials, or order endpoints. |
| Data provider | Whether tick-level bid/ask or aggregated bars are available; timestamp/bar semantics; timezone and DST handling; symbol mapping; correction policy; history depth; volume definition; licensing; heartbeat/reconnect | Define `MarketDataProvider` returning normalized `ForexQuote`/`ForexQuoteBar` with provenance. Use deterministic local fixtures. No provider SDK or network dependency in the core. |
| Account currency and conversion source | Pip value, notional, margin, P&L, daily loss, metrics, reporting | `AccountProfile` requires an explicit account currency and timestamped conversion-rate source. Missing or stale rates fail closed. |
| Leverage / margin policy | Position sizing, liquidation risk, hard limits | `AccountTerms` and instrument margin specs are explicit inputs. No leverage default; zero/missing/NaN/Inf rejects. |
| Trading strategy | Signals, research, lifecycle, production behavior | Strategy registry and lifecycle only. No active production strategy. Test strategies are deterministic fixtures. |
| Cost and financing model | Net edge, backtest realism, paper P&L, rollover accrual | `CostModel` and `RolloverSchedule` interfaces with versioned local fixtures; broker values are configuration, not code assumptions. |
| Execution semantics | Market/limit/stop behavior, slippage, partial fills, time-in-force, rejection handling | `BrokerCapabilities` describes what an adapter supports. Unknown capability fails closed. Local paper engine simulates documented semantics only. |
| Correlation/exposure limits | Aggregate currency exposure and correlated-pair risk | Explicit exposure groups and correlation matrix with provenance/version. Missing or stale inputs block new entries. |
| Calendar validity | 24/5 operation, daily breaks, holidays, weekend gaps, DST, rollover boundaries | Calendar objects carry source, version, valid-from/until, and instrument scope. Generic 24/5 is never substituted for the selected broker's calendar. |

A provider-neutral offline conformance suite now defines the required terms and replays symbol mapping, calendar boundaries, DST, holidays, stale/corrected quotes, precision, financing, commissions, order lifecycle, and reconciliation with fake adapters. Provider profile onboarding adds a versioned JSON format with provenance and effective dates for those terms. Once a broker/provider is chosen, supply its actual values in a profile and add provider-specific adapter tests. Selection must not enable live trading in this repository.

## 6. Staged migration path

1. **M0 — Assessment and safety contract (this change).** Record the architecture, safety gates, assumptions, open decisions, and first milestone. Add secret/runtime ignore rules. No runtime behavior changes.
2. **M1 — Provider-neutral forex safety/domain kernel.** Implement the core value objects, interfaces, calendar, instrument catalog, account/cost/risk inputs, audit log, and one strict pre-trade gate. Local paper adapter only; no order routing.
3. **M2 — Data pipeline.** Add bid/ask normalization, quote-to-bar aggregation, validation/quality/cache, calendar-aware gap detection, versioned dataset manifests, and a local fixture provider. Preserve completed-bar and no-interpolation rules.
4. **M3 — Forex accounting and hard risk.** Add pip value, conversion, margin, commissions, slippage, financing/triple rollover, signed positions, aggregate currency exposure, correlated-pair limits, and invalid-calculation rejection.
5. **M4 — Backtesting and validation.** Port next-bar execution and chronological validation to bid/ask bars; handle session gaps, weekend gaps, costs, out-of-sample splits, walk-forward, robustness, and backtest/paper parity.
6. **M5 — Strategies, regimes, lifecycle, research.** Port pure indicators/strategies and the lifecycle ladder; inputs are session-aware and provenance-labeled; learning remains unable to alter hard risk or mode.
7. **M6 — Execution engine and paper operation.** Port durable intents, emergency STOP, pause/reconcile, unresolved-order blocking, idempotent fills, and restart recovery to a local paper broker. No live adapter.
8. **M7 — Persistence, monitoring, API, dashboard, desktop.** Add additive schema migrations, audit views, quote/session/financing health, reports, authenticated API, and paper-only UI status surfaces.
9. **M8 — Optional provider/broker conformance.** Offline capability contracts, deterministic fake adapters, provider profile onboarding, and fail-closed checks are implemented. Only after explicit selection, implement a read-only provider adapter and a sandbox/demo broker adapter behind reviewed configuration. Live trading remains outside scope.

Each milestone should keep source-compatible module boundaries where practical, add focused tests before broadening behavior, and avoid deleting or weakening an existing safety invariant.

## 7. Scoped first milestone (M1)

**Goal:** prove that a new order can be evaluated safely without choosing a broker or connecting anywhere.

Planned modules:

- `app/config/` — paper-only `Settings`, explicit `AccountProfile`, `RiskLimits`, and configuration validation. No production defaults for account currency, leverage, pair, calendar, cost model, or strategy.
- `app/data/` — `ForexQuote`, `ForexQuoteBar`, `QuoteFreshness`, `MarketDataProvider` interface, and validation helpers using `Decimal` for money/price precision.
- `app/universe/` — `CurrencyPairSpec`, `InstrumentCatalog`, strict alias resolution, quantity min/step checks, and spec versioning.
- `app/calendar/` — `TradingCalendar`, `MarketStatus`, instrument-scoped windows, holidays, daily breaks, weekend gaps, DST-aware UTC instants, and validity/expiry checks.
- `app/accounting/` — interfaces/value objects for account currency, timestamped conversion rates, pip value, margin, cost model, and rollover schedule. Full accounting arrives in M3.
- `app/risk/` — one `PreTradeGate` with an explicit decision/audit record. It rejects new orders on: live mode, kill switch, unknown/inactive instrument, missing spec, closed/unknown/expired calendar, stale/future/incomplete quote, crossed quote, excessive spread, invalid or non-finite risk inputs, missing account terms/cost model/conversion rate, leverage or exposure-limit breach, and any unresolved order.
- `app/execution/` — `BrokerCapabilities`, `BrokerAdapter` protocol, and `LocalPaperBroker` with no network calls, credentials, or real-money order path. The gate is the only route to an order intent.
- `app/audit/` — structured, secret-safe decision log.
- `fixtures/` — synthetic currency codes, one 24/5 calendar with a daily break and a DST transition, deterministic quotes, and explicitly labeled test-only account/cost/risk values.

Focused tests:

- pair identity, base/quote extraction, precision, pip size, contract size, min/step quantity, alias mismatch, unknown instrument;
- quote validation, stale/future timestamps, crossed bid/ask, missing side, and executable ask/bid versus derived mid;
- open/closed status for normal sessions, daily break, weekend gap, holiday, DST change, expired calendar, and unavailable instrument;
- pip value/margin/conversion with valid and invalid account terms;
- fail-closed matrix for all rejection reasons above, with audit records and no order intent;
- live-mode and kill-switch refusal, unresolved-order blocking, and no-fallback-on-missing-input behavior;
- static tests that first-party code contains no credentials, live order endpoints, `live_trading=True`, or network calls in the default path.

**M1 acceptance:** `pytest` passes; every fail-closed condition blocks new orders; all rejection decisions are auditable; no production default selects a broker/provider/account currency/leverage/strategy; no external connection is attempted; live trading remains disabled.

## 8. Verification and non-goals

For every later code change, run a focused test module first, then the full suite, `compileall`, and a secret/live-endpoint scan. Maintain adapters behind local fixtures so tests remain deterministic and offline. Provider-neutral conformance and profile-onboarding tests are already required; add provider-specific adapter tests only after a provider is selected.

Explicitly out of scope for the current work: real-money account connectivity, live order placement, stored credentials, profitability claims, exchange-certified execution, tick-level market making, and a production trading strategy. “24/5” is a target operating shape; the selected broker's explicit calendar always wins.

## Implementation status (updated)

The staged plan above remains the design authority. The repository now contains:

- `app/`: the M1 provider-neutral forex safety/domain kernel, with strict fail-closed pre-trade gating and local simulated intent receipts only.
- `axiom_forex/`: the first coherent full-architecture adaptation, including data, instruments, calendars, indicators, strategies, regimes, validation, tournaments, lifecycle, accounting, aggregate risk, bid/ask backtesting, local paper execution, persistence, monitoring, API, dashboard, reporting, and desktop view models.
- `desktop/` and `scripts/`: pure operator contracts and offline verification entry points.
- `docs/FOREX_ADAPTATION.md` and `docs/RUNNING_SYSTEM_BOUNDARY.md`: implementation and isolation details.

All concrete values are local fixtures or explicit injection points. The live bridge remains disabled; no external broker/data-provider adapter is active. Offline Stage M8 conformance contracts, fake adapters, provider profile onboarding, and fail-closed checks are implemented; real adapter conformance remains future work and must be reviewed separately without interrupting the existing live operation.

## Integration status (updated)

The `app/` M1 kernel and `axiom_forex/` architecture have been connected through one canonical offline paper workflow. `ForexRuntimeSettings` now wraps `app.SafetySettings`; `ForexCostModel` implements the M1 cost protocol; `ForexExecutionContextFactory` and `ForexExecutionPlanner` are shared by backtests and paper operation; `ExecutionConfig` is the single execution configuration; `DurableAuditLog` is the single audit persistence path; and `PaperStateRecovery` rebuilds paper-engine and portfolio state from SQLite after restart. The fixture workflow uses explicit OOS qualification, `StrategyRouter`, the M1 gate, `LocalPaperBroker`, durable orders/fills/positions, and one workflow snapshot for API, dashboard, report, and desktop surfaces. Focused integration tests cover end-to-end execution, restart/unresolved-order behavior, and operator-surface consistency. Research backtests remain clean isolated simulations. An offline provider/broker conformance layer now blocks the workflow on missing or inconsistent capability terms and replays deterministic forex edge cases. Provider profile onboarding adds a versioned, provenance-bearing document format with its own fail-closed workflow gate; real adapter conformance remains future M8 work.

## Conformance status (updated)

The offline provider/broker conformance layer in `axiom_forex/conformance/` adds versioned market-data and broker capability contracts, deterministic fake adapters, fail-closed checks, and a durable conformance report. The paper workflow refreshes and persists that report and blocks before qualification, routing, or execution when it fails. Health, `verify()`, `/status`, `/workflow`, `/conformance`, desktop, dashboard, report, and CLI surfaces include the same result.

The layer covers supported pairs and aliases, quote/bar semantics, timestamps and precision, order types, sizing increments/minimums, partial fills, cancellation, reconciliation, trading hours, financing, account-currency conversion, idempotency, unknown outcomes, and restart recovery. Fixtures explicitly replay bid/ask spreads, stale quotes, gaps, daily breaks, DST changes, holidays, weekend gaps, validity bounds, unavailable instruments, rejected orders, partial fills, and unresolved orders. No real provider or broker adapter, credential, external account connection, live order path, deployment, or runtime-setting change was added. See [the conformance contract](PROVIDER_CONFORMANCE.md) for the exact provider-specific values still to be supplied.

## Provider profile onboarding status (updated)

Versioned JSON profiles now separate market-data, broker, instrument, calendar,
account/risk, cost/financing, and recovery terms. `profiles/offline_fixture_profile.json`
is explicitly synthetic and reproduces the passing offline workflow; `profiles/provider_onboarding_template.json`
contains all 37 required values with empty values and empty provenance. Every
term requires documentation/source, an effective date or source version, a
fixture/demo/production class, and review/expiry metadata. No production
defaults are invented.

Strict parsing reports missing, invalid, stale, contradictory, and
incompatible fields. Compatibility checks compare data and broker identity,
pair specs, timeframes, quote semantics, account currency and conversion
rates, calendar and DST terms, costs, margin, order capabilities, and recovery
policy. A safe operator report exposes profile identity, validation status,
activation status, and field diagnostics without term values, credentials, or
sensitive account values.

The offline workflow refreshes and persists this report and blocks before
qualification, routing, or execution when validation fails or activation is
not allowed. Health, `verify()`, `/status`, `/profile`, `/workflow`, desktop,
dashboard, report, and CLI surfaces include the same safe result. Only an
explicitly synthetic local-paper fixture profile can activate; demo and
production profiles are review artifacts and never enable external execution.

Focused profile tests cover round-trip loading, versioning, incomplete and
contradictory profiles, compatibility failures, stale terms, redaction,
workflow blocking, and operator surfaces. The full offline suite currently
passes with 605 tests. No external provider, credentials, account access,
network calls, live orders, deployment, runtime-setting changes, or
running-system changes were added. See [Provider profile onboarding](PROVIDER_PROFILE_ONBOARDING.md).

## Multi-pair research evaluation status (updated)

`axiom_forex/evaluation/` adds a research-only evaluation path for several
imported pairs at once: UTC-grid alignment with explicit per-pair calendar
closures and missing intervals, a multi-currency book with explicit conversion
and shared-currency aggregate exposure, chronological train/validation/test
separation, rolling walk-forward folds confined to the development segment,
in-process custody that rejects any reused or overlapping out-of-sample window,
next-bar bid/ask fills with a look-ahead guard, pair/session/regime/portfolio
metrics, and a qualification decision that can never activate a strategy. Data
gates block synthetic fixtures, demo-only data, mixed classes, failed manifests,
short or incomplete series, out-of-session bars, and missing calendar or
correlation terms. `/evaluation`, `status()`, the Markdown report, `verify()`, and
the `evaluate` CLI command expose the same result; the paper order stream is
untouched and no order can be created by this path. See
[the research evaluation guide](RESEARCH_EVALUATION.md) for terms, reason codes,
and the provider/broker values that are still required.
