# Running-system boundary

The user's existing live trading operation must remain untouched. All work in this repository is isolated code and documentation.

## Read-only integration-boundary inspection performed

- Listed processes, listening sockets, and service units visible inside the sandbox.
- No AXIOM/arena/trading-bot process, trading service, or account integration was visible in the sandbox.
- Only ordinary system ports/services were visible; no service was identified as the live trading bot.
- No credentials, environment values, account state, broker settings, runtime configuration files, or live order state were inspected or copied.
- No process was stopped, restarted, reconfigured, interrupted, deployed to, or connected to.

An external live operation may exist outside this sandbox. It is outside the repository boundary and was not inspected or altered.

## Repository isolation rules

- New code lives in `app/` and `axiom_forex/`; the latter is not imported by a running bot.
- Concrete adapters are explicit injection points: `MarketDataProvider`, `BrokerAdapter`, `TradingCalendar`, `ConversionRateBook`, `CostModel`, and `RolloverPolicy`.
- The only active broker path is `LocalPaperBroker`, which returns a `SIMULATED_INTENT` receipt. No external broker client, credential reader, or network order path exists.
- `axiom_forex/conformance/` contains offline capability contracts and deterministic fake adapters only. Its report is computed from explicit local values and never reads running-system configuration or account state.
- `profiles/` and `axiom_forex/profiles/` contain offline onboarding documents and validation code only. Profiles never contain credentials, never enable external execution, and demo/production profiles cannot activate the workflow.
- `axiom_forex/ingest/` and `imports/` contain an offline canonical CSV importer and its format example. The importer reads a local file only, makes no network call, reads no credentials or account state, and never enables external execution. Imported data is research/backtest-only (never used for paper fills) and is stored with an explicit `USER_SUPPLIED_*` data class and manifest hashes.
- Persistence requires an explicit SQLite path. The fixture platform defaults to an in-memory database; it does not open the running system's database.
- The WSGI API is an in-process callable with only GET read-only endpoints. No server is started and no order-control endpoint exists.
- `ForexRuntimeSettings.live_bridge_enabled` must remain false and `research_only` must remain true.
- `SafetySettings` cannot be constructed in live mode, and a LIVE request to `LocalPaperBroker` is blocked and audited.
- The **Market Access Token** setting is a desktop-only input for a *future* market-data provider. Its value is written to the operating-system credential vault and nowhere else: no repository file, profile JSON, `.env`, SQLite row, log line, audit event, report, or API response carries it. There is no plaintext fallback -- if no OS vault is available the save is refused with an explanation. This milestone reads no credential from the running system, adds no provider, and makes no network request; the setting is not connected to data-provider traffic and cannot be turned into one (`MarketAccessTokenStore.for_provider_request` refuses).

## Required review before any external integration

- Supply the exact values listed in [the conformance contract](PROVIDER_CONFORMANCE.md) using [the profile onboarding process](PROVIDER_PROFILE_ONBOARDING.md), then conformance-test the selected broker's symbols, specs, calendars, DST/holiday/break behavior, quote semantics, costs, financing/triple-rollover rules, margin/leverage, account currency, pip value, and order lifecycle.
- Keep credentials outside this repository and verify independent audit/recovery behavior.
- Review and approve a separate migration plan that does not interrupt the current live operation.
- Do not treat repository tests or synthetic backtests as live-trading approval.
