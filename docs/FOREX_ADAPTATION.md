# AXIOM-FOREX architecture adaptation

This guide describes the first coherent forex adaptation through AXIOM's existing architecture. It is a research and paper-trading implementation, not a profitability or live-readiness claim.

## Layer mapping

| AXIOM layer | Forex adaptation | Isolation/safety behavior |
| --- | --- | --- |
| Configuration and CLI | `axiom_forex/config`, `axiom_forex/cli.py`, `scripts/verify_forex.py` | Explicit fixture settings; no environment or credential reads; offline commands only. |
| Data providers/normalization/validation | `axiom_forex/data` | Strict provider-alias resolution, timestamped bid/ask quotes, no interpolated bars, calendar-aware gap/staleness checks. |
| Instrument universe | `app/universe`, `axiom_forex/universe` | Versioned currency-pair specs, pip/tick/precision, contract size, quantity steps, margin multiplier, correlations, calendar identity, provider aliases. |
| Calendars | `app/calendar`, `fixtures/m1.py` | Explicit UTC windows, daily breaks, holidays, DST-aware local times, weekend gaps, validity bounds, instrument availability. Generic 24/5 is fixture-only. |
| Indicators/strategies/regimes | `axiom_forex/indicators`, `strategies`, `regimes` | Prefix-only completed bars, research-only mid prices, no production strategy selected. |
| Validation/tournaments/learning | `axiom_forex/validation`, `tournament`, `learning` | Chronological splits, walk-forward folds, one-use locked test, lifecycle ladder, router that requires active approved strategies. |
| Accounting/risk | `axiom_forex/portfolio`, `risk` | Account-currency conversion, per-side commission/slippage, explicit swap-rate cards and cutoff instants, configurable triple rollover, leverage/margin, aggregate currency exposure, explicit correlation matrix. |
| Backtesting | `axiom_forex/backtesting` | Next-bar bid/ask execution, no final-bar look-ahead, no forced liquidation, costs/gaps/calendar checks, OOS validation. |
| Execution/brokers | `app/execution`, `axiom_forex/execution`, `brokers` | Strict M1 pre-trade gate, local simulated intent receipts, idempotent fills, unresolved-order no-resubmit, external boundary disabled by default with an opt-in MT5 adapter behind it. |

| Desktop settings | `desktop/`, `axiom_forex/api/wsgi.py` | Request contract, pure view models, and the Market Access Token setting (OS credential vault only; masked status; no plaintext fallback, no provider use). |
| Monitoring/API/reporting/desktop | `axiom_forex/monitoring`, `api`, `dashboard`, `reporting`, `desktop/` | Health/anomaly checks, read-only authenticated GET endpoints, pure HTML/Markdown/view models, no live controls. |

## Safety chain

1. `SafetySettings` and `ForexRuntimeSettings` enforce research/paper mode and a disabled live bridge.
2. Data quality fails closed for invalid, stale, future, incomplete, out-of-session, overlapping, or missing bars.
3. `PreTradeGate` validates settings, state, unresolved orders, account/risk/cost/rollover/rate configurations, instruments, calendar, quote freshness, spread, conversion rates, and hard limits.
4. `LocalPaperBroker` blocks LIVE requests and returns only a `SIMULATED_INTENT`. A live order path exists only inside `axiom_forex/brokers/mt5/`, is not attached to the fixture platform, and requires an explicit local `TRADING_ENABLED` mode, in-process arming naming the account, an allowed pre-trade decision, and an approved active strategy; see `docs/MT5_INTEGRATION.md`.
5. `PaperExecutionEngine` is idempotent and treats settlement failures as unresolved orders, which block later orders. Idempotency fails closed: a reused request id is only answered as a duplicate when the stored payload can be read and matches (side, quantity, close target, evaluation instant); an unreadable stored payload, a different payload, or an unvalidated request raises instead of silently dropping or re-stamping the order. Rejected orders carry no receipt, so they store a payload digest for the same comparison. `cancel(request_id, cancelled_at=...)` moves a not-yet-filled order to `CANCELLED` (idempotently, with an operator-supplied instant), refuses to "cancel" a filled order, and releases its reservation.
6. Aggregate risk uses an explicit correlation matrix; missing correlation data fails closed.
7. All audit/persistence values pass through sanitization; credential-like keys and URL-like values are redacted.

## Close semantics (position-aware and fail-closed)

A risk-reducing order is never inferred from its side. `OrderRequest.close_intent` is
a `CloseIntent` naming the exact position (`position_id`, `account_id`), the
quantity to close, and optionally the pair and quantity unit; a SELL without one is
an ordinary entry (a new short). The gate matches the intent against
`GateContext.open_positions`, a snapshot the ledger builds from its own book with
in-flight close reservations already subtracted, and refuses with a specific code:

| Check | Rejection |
| --- | --- |
| Intent malformed, or its quantity differs from the order's | `INVALID_CLOSE_INTENT` |
| Position not open | `CLOSE_POSITION_NOT_FOUND` |
| Position (or intent) belongs to another account | `CLOSE_ACCOUNT_MISMATCH` |
| Position is on another pair | `CLOSE_PAIR_MISMATCH` |
| Order moves with the position instead of against it | `CLOSE_SIDE_MISMATCH` |
| Wrong quantity unit | `CLOSE_QUANTITY_UNIT_MISMATCH` |
| Quantity exceeds what is left after other in-flight closes | `CLOSE_QUANTITY_EXCEEDS_POSITION` |
| Projected exposure would not fall, or margin would be added | `CLOSE_INCREASES_EXPOSURE` |

Projected exposure for a close is computed from the ledger position and the portion
being closed (`calculate_risk(close_position=...)`): gross exposure falls by the
closed notional, required margin is zero, the released margin is reported, currency
and correlation-group deltas carry the position's sign inverted, and effective
leverage can therefore reach zero. Entry limits (open positions, daily loss, trades
per day) and the absolute entry caps apply only to entries; a close must instead
*strictly* reduce gross exposure. Every unrelated check -- safety settings, system
state, unresolved orders, instrument/spec, calendar, quote freshness, spread, and
conversion rates -- still runs for a close before the intent is even matched.

Partial closes are first-class in the ledger: `ForexPortfolio.close(..., quantity=...)`
closes exactly that slice, keeps the position open with the remainder (exposing
`quantity`, `original_quantity`, `closed_quantity`), allocates the matching share of
the entry costs to the closed trade (`entry_costs_account`), and accrues financing
only on the slice being closed, so slices add up to the same totals as one full
close. All checks and cost/rate lookups happen before the first mutation, so a
refused or failing close leaves the book untouched and can be marked unresolved
without a half-applied position.

Identity and recovery:

- the position snapshot persists `account_id`, `original_quantity`,
  `original_base_units`, `entry_costs_total_account`, the *remaining* quantity, and
  the unallocated entry costs, so a restart restores a partially closed position
  exactly;
- the approved close target travels in the order receipt (`close_position_id`,
  `closed_base_units`, `exposure_reducing`), so settlement can prove it is closing
  the position the gate approved and refuses a mismatched or missing target
  (`close_position_mismatch`, `close_without_approved_position`);
- a close is reserved from the moment it is simulated and released only when the
  record reaches a final state. The reservation is durable (see *Durable order
  reservations* below), so the promise holds across engine instances and
  processes, not just within one. An unsettled or unresolved order blocks further
  orders (`UNRESOLVED_ORDER_PRESENT`), and a close validated against a stale context
  is still refused at the ledger (`close_quantity_exceeds_position`), so two closes
  can never consume the same quantity;
- each partial close slice is persisted as its own row (keyed by position and close
  instant) so the position index keeps every slice.

## Durable order reservations (schema v2)

The close reservations described above were process-local: a dictionary in
`PaperExecutionEngine`. Two engine instances -- another process, a second service,
a re-opened GUI -- each kept their own dictionary, so both could read "0.10 left"
and each promise 0.06 of it. SQLite now holds the promise instead.

`OrderReservationBook` (`axiom_forex/execution/reservations.py`) is the single
place where an order's quantity and idempotency key become durable. Schema
migration **v2** adds two additive tables, leaving v1 data untouched:

| Table | Purpose |
| --- | --- |
| `order_reservations` | One row per request id: kind (`CLOSE`/`ENTRY`), state, position, quantity, exposure, fingerprint, and `released_at` (`NULL` while the promise is live). The primary key is the request id, so a duplicate insert is impossible. |
| `position_guards` | The smallest remaining quantity any engine has claimed for a position. It only ever decreases, so a stale engine cannot raise the ceiling another engine already lowered. |

Quantities and exposure are exact integers scaled by `1e-8`
(`RESERVATION_UNITS`), so every sum and comparison is integer arithmetic in
SQLite -- never floats, never decimal text. A value that does not fit that grid is
refused (`unsupported_quantity_precision`) rather than rounded.

**Transaction model.** `ForexRepository.transaction()` opens `BEGIN IMMEDIATE` on
a connection in autocommit mode (`journal` untouched, `busy_timeout` 30 s). Taking
the write lock *before* the reads is what makes the decision sound: the reads
(reserved quantity, reserved exposure, unresolved orders, durable idempotency key)
cannot be invalidated before the commit. Nested calls join the open transaction, so
the whole unit commits once or rolls back once. Measured behaviour of two
simultaneous writers: with `BEGIN IMMEDIATE` the loser waits and then sees the
winner's reservation and is cleanly refused; with a deferred `BEGIN` the loser dies
with `database is locked` instead -- no orderly refusal, and no protection if the
reads happen to both fit.

One `submit` is therefore a single atomic unit: durable idempotency lookup,
unresolved-order check, limit check, reservation insert, gate simulation, order-row
write, and reservation activation. `simulate()` runs inside the transaction and
returns the order payload that is persisted; anything it raises other than
`TradingBlocked` rolls the whole unit back.

**Limit arithmetic, inside the transaction.**

- *Close*: `SUM(quantity_units)` of live `CLOSE` rows for the position plus the
  request must not exceed the guard (which is first lowered to the caller's claimed
  remaining quantity). Exceeding it is `close_quantity_exceeds_position`.
- *Entry*: `SUM(exposure_units)` of live `ENTRY` rows plus this request's notional
  must not exceed the configured cap (`entry_exposure_limit_exceeded`). Notional is
  `quantity x` the executable side of the caller's own quote (ask for a buy, bid
  for a sell) -- no mid price is invented. When a cap is configured and the request
  carries no complete quote, the order is refused (`entry_exposure_unpriced`).

**Order-state behaviour.**

| Outcome | Reservation | Order row |
| --- | --- | --- |
| Accepted (`SIMULATED`) | inserted, live | written in the same transaction |
| Refused by the gate (`TradingBlocked`) | released in the same transaction | written by the engine immediately after |
| Refused by a limit or by an unresolved order | never inserted | written by the engine immediately after |
| Cancelled | released with the order row, in one transaction | updated |
| Filled | released in one transaction with the order row; a closed position also lowers the guard to what is left | updated |
| Unresolved | kept live (the quantity stays spoken for) and blocks new orders | updated |
| Failed transaction | rolled back: no reservation, no order row | none |

Releasing keeps the row and stamps `released_at`, so the history of a promise
survives. A replayed `request_id` returns the durable answer -- the same
simulation id for the same payload, and
`request_id_reused_with_different_payload` for a different one. A request id cannot
be reserved twice even if every in-process cache is empty.

**Recovery.** `PaperStateRecovery` restores the order book from `orders`, then
refuses to start when the durable records disagree: a live reservation without a
matching, still-open order (`recovered_reservation_without_order`), a live
reservation whose order is already final
(`recovered_reservation_for_final_order`), a live close reservation for a position
the restored ledger does not hold
(`recovered_reservation_for_unknown_position`), or an in-doubt order with no
reservation (`unresolved_order_without_reservation`). Recovery also refuses a
snapshot that predates its own fills, as before.

**Deliberate limits of this step.**

- The unresolved-order rule is checked before the limit arithmetic, so a losing
  racer is usually refused with `UNRESOLVED_ORDER_PRESENT`; the quantity and
  exposure limits are the backstop that binds when the competing promise is not an
  order in doubt (for example a live reservation left by another bundle, or a
  release write that never landed).
- Fills are written by the settlement transaction (see below); position *snapshots*
  that the reporting surfaces read are still refreshed by the caller on its own
  schedule. The atomic pair is order + fill + ledger snapshot + reservation + guard
  + audit event.
- One connection per process is assumed, as before (single-writer offline paper
  mode); threads sharing one connection are not coordinated.
- Several engine bundles share one database (the workflow's own bundle, the
  out-of-sample bundle). Each bundle's book is its own; recovery restores the
  platform bundle's snapshot and validates every fill against *its own* settlement
  snapshot, so a fill from another bundle is checked where it was written rather
  than being replayed into this one.

## Atomic settlement

Settlement (`PaperExecutionEngine.settle`) is the moment paper money moves, and it
is a single `BEGIN IMMEDIATE` transaction covering six records:

| Written together | Notes |
| --- | --- |
| Order row | `SIMULATED` -> `FILLED`, with the fill instant |
| Fill row | keyed by simulation id, so one simulation can have exactly one fill |
| Positions index | open rows, closed slices, partial-close remainders |
| Portfolio snapshot (`portfolio-settlement:<simulation_id>`) | cash, realized P&L, daily counters, cost and rollover totals, `settlement` provenance |
| Reservation + position guard | reservation released, guard lowered to the remaining quantity |
| Audit event | `PAPER_ORDER_FILLED`, written inside the same transaction |

The ledger is applied to a **clone** first. The live book is replaced only after
the commit, and from the committed snapshot itself, so memory cannot drift from
the record: a failure at any point leaves the live book exactly as it was. The
settlement snapshot is written with `allow_unmarked=True`, meaning equity, gross
exposure, margin, and leverage are recorded as `null` rather than inventing a mark
price for an instant with no bar; `restore_snapshot` needs none of those fields.

**Exactly-once.** The fill id is the simulation id and its row is unique. Replaying
a settlement returns the committed fill and re-asserts the committed snapshot
instead of applying P&L, fees, rollover, or position changes again. A replay whose
price, quantity, instant, action, or position differs from the committed fill is
refused as `settlement_conflict`; a replay of an order that is final but has no fill
is `filled_order_without_fill`.

**Audit consistency.** `DurableAuditLog.record` writes and commits before the event
enters the in-run cache. Inside `DurableAuditLog.atomic()` (which settlement,
submit, cancel, and rejection all use) events are staged and appended to the cache
only after the commit; a rollback drops them. Recording inside somebody else's
transaction raises `audit_record_requires_atomic_unit` instead of risking a cached
event with no row. Recovery calls `rebuild_cache()` so a restarted process starts
from the committed rows.

**Recovery reconciliation.** The restored book is the newest whole-platform
`portfolio` snapshot (a per-settlement snapshot is only a fallback when nothing
else exists). Recovery then refuses to start when the records disagree: a fill
without its settlement snapshot (`settlement_snapshot_missing`), a settlement
snapshot whose position or closed trade does not match the fill
(`settlement_snapshot_missing_open_position` / `..._closed_trade`), a book older
than the newest settlement (`recovered_portfolio_predates_settlement`), a book
missing a position that settlement left open
(`recovered_portfolio_missing_settled_position`), plus the reservation checks
above. A database with orders but no fills recovers into an empty book: no fill
means nothing was applied.

## Entry-exposure cap (declared, never assumed)

The reservation book refuses a new entry unless it has a declared cap, and the cap
comes from the active runtime profile (`ForexRuntimeSettings.entry_exposure_limit`)
-- never from a default invented in code:

| Profile state | Entry | Close |
| --- | --- | --- |
| `EntryExposureLimit` in the account currency | allowed while the live reservations plus this notional fit | allowed |
| Nothing declared (`None`) | `ENTRY_LIMIT_NOT_CONFIGURED` | allowed |
| Declared in another currency | `ENTRY_LIMIT_INCOMPATIBLE` | allowed |
| Malformed (non-positive, unversioned, unlabeled) | platform refuses to start; the book reports `ENTRY_LIMIT_INVALID` | allowed |
| Request carries no usable quote or no conversion rate | `entry_exposure_unpriced` | n/a |

The notional is measured in **account currency** through the ledger's own
`entry_notional_account(quantity, price, at)`: spec base units x the executable
side of the caller's quote (ask for a buy, bid for a sell) x the timestamped
quote-to-account rate, rounded *up* to the store's 1e-8 grid. No account currency
and no conversion rate are assumed; a missing rate fails closed.

The offline fixture profile declares `250 CCC` with
`data_class: SYNTHETIC_FIXTURE`, `source: OFFLINE_FIXTURE_PROFILE`,
`version: synthetic-entry-limit-v1`. The label travels into
`ForexRuntimeSettings.audit_view()`, `ForexPlatform.entry_limit_view()`, and the
`verify` command output (`entry_exposure_limit`, plus the
`entry_limit_declared` / `entry_limit_provenance_labeled` checks), so a synthetic
number can never be mistaken for an operator's risk policy. Operators pass their
own `EntryExposureLimit`; the repository ships no production value.

## Market Access Token setting

A desktop settings surface for the credential a *future* market-data provider
would need. It is explicitly **not** a broker trading credential, it selects no
provider, and nothing in this build sends it anywhere.

**Where it appears.**

| Surface | What it shows |
| --- | --- |
| `desktop/market_access.py` | The setting itself: `MarketAccessTokenStore` (vault-backed) and `MarketAccessSettings` (the unlocked settings surface) |
| `ForexPlatform.market_access_settings()` / `market_access_status()` | The desktop application's entry points |
| `desktop/viewmodels.py` (`/desktop`) | `market_access`: configured flag, mask, storage health |
| `GET /settings/market-access` | Authenticated JSON status; the value is never in it |
| `GET /dashboard` | A "Market access setting" table: purpose, configured, storage, fallbacks, provider selected, used for requests |
| `GET /report` | The same facts as Markdown (`## Market access setting`) |
| `GET /status` | `market_access`, refreshed live rather than from a cached workflow snapshot |

**How storage works.** `CredentialVault` is a narrow protocol
(`describe`/`available`/`read`/`write`/`erase`); `KeyringCredentialVault` is the
only real implementation and speaks to the operating system's store through
`keyring` (Windows Credential Manager, macOS Keychain, Secret Service), under the
service name `AXIOM-FOREX` and the entry
`axiom_forex.market_data.market_access_token`. There is **no** file, `.env`,
profile, SQLite, or in-memory-forever fallback: if the vault reports unavailable
(or the backend is one of `keyring`'s fail/null stubs), `save()` raises
`SecureStorageUnavailable` *before* it looks at the value, with a reason the
interface can show. `clear()` refuses the same way rather than pretending a
token it cannot reach was removed.

**Enter / replace / clear.** `MarketAccessSettings.set_token(...)` and
`clear_token(...)` require the operator's local API bearer (compared in constant
time); without it they raise `SettingsAccessDenied` and touch neither the vault
nor the audit trail. `save()` writes once and drops the local reference;
replace simply overwrites the same entry (an audit event distinguishes
`enter` from `replace`); `clear()` is idempotent. A token must be a single
non-empty line of 8-4096 characters with no whitespace or control characters --
structural safety only, because the real format belongs to a provider that has
not been chosen.

**Masking.** `status()["configured"]` is a boolean derived from *whether the
vault holds an entry*, never from its content. The display string is the fixed
`"********"` (empty when nothing is configured) -- it is not a prefix, suffix, or
length of the value, so two different tokens produce exactly the same mask.
There is deliberately **no accessor** that returns the secret.

**Redaction.** Audit records are built from an allow-list of non-secret fields
(`action`, `reason`, `configured`, `storage`, `backend`) plus fixed provenance;
a caller cannot smuggle a value through them, and the shared audit sanitizer
redacts credential-shaped keys as a second line of defence. The payload keys are
chosen so that no *label* is redacted away (`broker_trading_use`, not
`broker_credential`, which the sanitizer would blank out). Reports, the
dashboard, the desktop view model, and every API response carry only presence
and storage health.

**Disconnected by construction.** No provider is selected, so
`for_provider_request()` raises (`provider_not_selected`, or
`market_data_requests_disabled` even if a provider were marked selected) and
audits the attempt. Tests assert that running the paper workflow makes no vault
write, reads only the setting's own presence flag, and that the data-provider
object holds no reference to the store. There is no "test token" button and no
network call anywhere in this path.

**Offline fixtures keep working without a token** -- configured, absent, or
unavailable storage all leave the workflow, health, and verification output
unchanged.

## Forex-specific behavior

- Currency-pair identity, base/quote, pip size, tick size, quote precision, contract size, min/step/max quantity, margin multiplier, status, calendar identity, and spec version are explicit.
- BUY executes at ask; SELL executes at bid. Mid prices are used only for research indicators.
- Costs are explicit per side: commission as a fraction of notional, slippage in pips, financing from swap-rate cards at explicit cutoff instants with a configurable triple-rollover weekday.
- Currency conversion uses timestamped rate books; missing/stale rates fail closed. Pip value, notional, margin, PnL, and exposures are normalized to the account currency.
- Long and short positions, leverage, margin, correlated-pair exposure, aggregate currency exposure, daily loss, trade count, and open-position limits are modeled. The two `*_today` counters belong to one **trading day**: the ledger takes the day from the injected trading calendar's own timezone (the broker's local day, e.g. `America/New_York` in the fixtures) and rolls the counters when a fill crosses that boundary, so a loss day is not carried for the life of the process and the daily limits do not silently become lifetime limits. The day and the basis (`CALENDAR_LOCAL_DATE:<tz>`, or `UTC_CALENDAR_DATE` when no calendar is injected) are persisted in the portfolio snapshot, so a restart resumes the same day.
- Calendars are explicit UTC windows derived from DST-aware local times; daily breaks, holidays, weekend gaps, validity bounds, and instrument availability are never inferred from a generic 24/5 assumption.
- Backtests use completed bars for signals, next-bar bid/ask open prices for fills, apply costs, classify gaps, cancel final-bar signals, and do not force liquidation.
- Validation uses chronological splits, walk-forward folds, a one-use locked test set, tournaments, lifecycle promotion, and a router; no strategy is active by default.

## Open dependencies

No broker, data provider, account currency, leverage, or strategy has been chosen for production. `axiom_forex/dependencies.py` records the decisions that remain open, and [the conformance contract](PROVIDER_CONFORMANCE.md) lists the exact provider-specific values to supply once a broker/provider is selected. The fixture values (`AAA/BBB`, `CCC`, synthetic 24/5-style calendar, synthetic rates and swap cards) are clearly labeled test data only.

## Integrated offline paper workflow

The architecture now has one canonical fixture-backed path from raw bid/ask payloads to operator-visible health/status/report data:

1. `ForexDataPipeline` resolves provider aliases through the versioned `InstrumentCatalog`, normalizes raw bars and quotes with `DataNormalizer`, rejects unusable quality without interpolation, builds a verified dataset manifest, and installs the normalized `SyntheticForexProvider` (including its normalized quotes).
2. `ForexRuntimeSettings` wraps the canonical `app.SafetySettings`; `ForexCostModel` implements the M1 `CostModel` protocol from one versioned `CostModelTerms`; `ExecutionConfig` is shared by backtests and paper workflows. `BacktestConfig` is a compatibility alias, not a second implementation.
3. `ForexExecutionContextFactory` is the only `GateContext` builder. `ForexExecutionPlanner` is the only signal-to-`OrderRequest`, next-bar bid/ask fill price, action, position id, and calendar-state path. Both backtests and the workflow use them.
4. `qualify_fixture_strategy(...)` creates an ephemeral APPROVED lifecycle from explicit chronological OOS validation; `StrategyRouter.select(...)` and `StrategyRouter.route_signal(...)` route one fixture signal. The default lifecycle registry stays in RESEARCH, so no production strategy is selected.
5. `PaperExecutionEngine` executes only through `LocalPaperBroker`. `DurableAuditLog` writes each sanitized decision once to SQLite. Orders, fills, open/closed positions, portfolio snapshots, monitoring events, backtests, and qualified lifecycles are persisted explicitly.
6. `PaperStateRecovery` rebuilds paper-engine idempotency state and portfolio state from SQLite after restart. Recovered unresolved orders block a new workflow before any order is submitted.
7. `PaperWorkflowResult` produces one immutable workflow snapshot. `status()`, `/health`, `/status`, `/workflow`, `/desktop`, the dashboard, and the Markdown report all use that same cached/persisted snapshot instead of independently recomputing a divergent status.
8. `execute_paper_workflow` refreshes the offline provider/broker conformance report after normalized data is loaded and blocks before qualification, routing, or execution when it fails. The passed/failed report is persisted as a `conformance` snapshot and included in health, status, workflow, `/conformance`, desktop, dashboard, report, and CLI views.
9. `execute_paper_workflow` first refreshes the provider-profile onboarding report and blocks when a profile is missing, invalid, stale, contradictory, not activated, or not explicitly synthetic. The safe report is persisted as a `profile` snapshot and exposed through `/profile`, status, health, verify, workflow, desktop, dashboard, report, and CLI. Demo and production profiles cannot activate external execution.

See [the conformance contract](PROVIDER_CONFORMANCE.md) for the exact provider-specific values that remain open and [provider profile onboarding](PROVIDER_PROFILE_ONBOARDING.md) for the versioned JSON format, provenance rules, and activation boundary.

Offline commands:

```bash
python -m pytest -q
python -m pytest -q tests/test_forex_conformance.py
python -m pytest -q tests/test_forex_provider_profiles.py
python -m pytest -q tests/test_forex_data_import.py
python -m compileall -q app fixtures axiom_forex desktop scripts tests
python -m axiom_forex conformance
python -m axiom_forex profile
python -m axiom_forex verify
python -m axiom_forex workflow --workflow-id local-smoke
python -m axiom_forex report
python -m axiom_forex import --import-path imports/SAMPLE_canonical_quote_bars.csv \
  --import-data-class USER_SUPPLIED_DEMO --import-source SAMPLE_CANONICAL_CSV \
  --import-source-version sample-v1 \
  --import-valid-from 2026-03-10T13:00:00Z --import-valid-until 2026-03-10T14:00:00Z
```

Use `--database-path /tmp/axiom-forex-paper.db` with the `workflow` command to exercise restart recovery against a local file. The command never starts a server and never connects to a broker or data provider.

Focused integration tests cover the end-to-end workflow, restart/unresolved-order recovery, and API/dashboard/report/desktop consistency. Focused conformance tests cover contracts, fake adapters, fail-closed mutations, workflow blocking, persistence, restart/no-resubmit, operator surfaces, and the offline CLI. Focused profile tests cover round-trip loading, versioning, incomplete/contradictory profiles, compatibility failures, stale terms, redaction, workflow blocking, and operator surfaces. Focused import tests cover canonical CSV import, DST/timezone resolution, gaps, duplicates and conflicts, invalid bid/ask values, incomplete bars, manifest integrity/tampering, provenance propagation, and fail-closed workflow behaviour. `tests/test_forex_audit_regressions.py` additionally pins every defect found by the independent correctness audits (P&L and cost accounting, point-in-time equity curves, evaluation bar re-validation and provenance, dashboard authorization, tournament selection basis, daily counter rollover, position-aware close validation, partial closes, concurrent and duplicate closes, cancellation, recovery after an unresolved partial close, idempotency fail-closed behaviour, and scenario-key validation). `tests/test_desktop_market_access.py` adds the Market Access Token suite (fake credential vault: save/replace/clear, invalid input, mask stability, access control, fail-closed unavailable storage, redaction across audit/API/report/dashboard and the SQLite file itself, and token-free fixture workflows). `tests/test_forex_atomic_settlement.py` adds the settlement suite: every record committed together, an injected failure at each write boundary (fill, positions, snapshot, release, audit) rolling the whole unit back, duplicate and conflicting settlement replays, partial-close retries, a worker process killed mid-transaction, audit-cache rollback, recovery reconciliation, and the declared/missing/incompatible entry cap. `tests/test_forex_order_reservations.py` adds the durable-reservation suite: schema v2 migration, SQL-level close and exposure limits, two live SQLite connections (duplicate ids, cancel, partial-close ceiling, rollback, restart recovery, contradictory records), and independent operating-system processes racing for one position, one request id, one exposure cap, and one write lock. `tests/test_mt5_settings.py` and `tests/test_mt5_adapter.py` add the MetaTrader 5 suite (fake `MetaTrader5` module and fake OS vault): local settings validation and secret-file rejection, vault-only password enter/replace/clear with mask stability and an unavailable-vault refusal, CLI prompts that never echo or print the value, terminal/account mismatch handling, symbol suffix/alias resolution and broker-term cross-checks, tick-built two-sided bars, the full retcode classification table, write-ahead ordering before `order_send`, partial fills, unknown outcomes that disarm trading and are never resent, restart recovery from deal/order history with bounded absence-of-evidence, netting-vs-hedging conflicts, ledger reconciliation, and the disabled external boundary. `tests/test_mt5_session.py` and the account-scope tests in `tests/test_mt5_adapter.py` pin the account model: the account is whichever one the terminal has selected (default `SELECTED_ACCOUNT`, so a real account is not refused for being real), `DEMO_ONLY` is an explicit opt-in, unconfirmed modes are reported rather than guessed, trading authorisation is separate from account mode and survives a terminal account switch, a switch drops the previous account's cached symbols/validation/recovery view and revalidates the new account's currency, leverage, margin mode, and execution terms, declared currency/leverage mismatches and profile-check items block submission with the exact reasons, order records and reconciliation are account-scoped so another account's unresolved orders or ledger rows are never reused, and identifiers/balances stay redacted. Also covered: missing-configuration blockers instead of invented orders, one qualified order with write-ahead ordering, rejected decisions, unknown-outcome disarm, and the session time budget. The full suite currently passes with 605 tests.

### Consolidation decisions

- `app/` remains the strict fail-closed safety/domain kernel; `axiom_forex/` composes it instead of replacing it.
- `safety_settings`, `m1_cost_model`, `GateContext` construction, and execution configuration no longer have competing implementations. `ForexPlatform.m1_cost_model` is a compatibility alias to `ForexCostModel`.
- Audit decisions are no longer copied from an in-memory log into SQLite after the fact: the gate, broker, and paper engine receive one `DurableAuditLog`.
- `run_backtest(...)` no longer replaces the durable workflow state; research backtests use clean engines while the operator workflow owns the persisted paper state.
- `/workflow`, `/conformance`, and `/profile` are authenticated, read-only GET endpoints; the desktop contract includes all three. No write, live, or order-control surface was added.

### Still separate or incomplete

- Research backtests remain clean, isolated simulations; they are intentionally not the durable live-shaped paper order stream. Both share the same config, cost model, context factory, planner, and local paper broker.
- Imported user data is research/backtest material only. It is adopted into a separate research slot (`use_imported_dataset`), is never used for paper fills, and blocks the paper workflow with `data_class_not_execution_eligible` while active. See [historical data import](HISTORICAL_DATA_IMPORT.md).
- The MT5 adapter (`axiom_forex/brokers/mt5/`) now supplies tick-to-bar reconstruction, broker term cross-checks, submission with write-ahead records, order/deal history, reconciliation, and restart recovery, verified offline against a fake `MetaTrader5` module. Production calendars, real costs/financing, account currency and leverage values, and strategies are still operator-supplied: the adapter reads them from the terminal and refuses on any mismatch, but they cannot be verified without the operator's own MT5 account. Deploying and enabling it stays an operator decision, documented in `docs/MT5_INTEGRATION.md`.
- The only active strategy is a clearly labeled fixture SMA used for OOS qualification and routing. Historical or simulated results are not profitability or live-readiness claims.

## Verification

Run the full focused test suite and offline CLI in a development environment:

```bash
python -m pytest -q
python -m pytest -q tests/test_forex_conformance.py
python -m pytest -q tests/test_forex_provider_profiles.py
python -m pytest -q tests/test_forex_data_import.py
python -m compileall -q app fixtures axiom_forex desktop scripts tests
python -m axiom_forex conformance
python -m axiom_forex profile
python -m axiom_forex verify
python -m axiom_forex workflow
python -m axiom_forex report
python -m axiom_forex import --import-path imports/SAMPLE_canonical_quote_bars.csv \
  --import-data-class USER_SUPPLIED_DEMO --import-source SAMPLE_CANONICAL_CSV \
  --import-source-version sample-v1 \
  --import-valid-from 2026-03-10T13:00:00Z --import-valid-until 2026-03-10T14:00:00Z
```

Tests cover normalization, DST/holiday/break/weekend-gap behavior, instrument versions, costs, rollover/triple rollover, aggregate risk, bid/ask fills, gap rejection, final-bar cancellation, OOS validation, idempotent/unresolved paper orders, provider/broker capability contracts, fake-adapter partial fills/rejections/unknown outcomes/restart recovery, conformance-driven workflow blocking, provider profile round-trip/versioning/validation/compatibility/staleness/redaction/workflow blocking, live-request blocking, persistence/redaction, health/anomalies, read-only API, dashboard/report/desktop views, and end-to-end fixture verification.

## Non-goals

- No claim of profitability or live-trading readiness.
- No credentials, OAuth, REST/WSS clients, live brokers, order routing, or account connection.
- No deployment to, restart of, or reconfiguration of the existing live system.
- No assumption that a broker's 24/5 calendar, costs, financing, margin, or symbols match the synthetic fixtures.

## Multi-pair research evaluation

Research evaluation lives in `axiom_forex/evaluation/` and is intentionally
separate from the paper workflow: it consumes canonical imported datasets for two
or more pairs, simulates fills in memory (next completed same-pair bar open), and
produces a labelled `AXIOM_FOREX_RESEARCH_EVALUATION` report. It never creates an
order, never writes to the paper order stream, and never enables external
execution. See [the research evaluation guide](RESEARCH_EVALUATION.md).
