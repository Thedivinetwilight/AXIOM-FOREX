# MetaTrader 5 integration

AXIOM-FOREX uses the **official MetaTrader 5 Python integration**
(`MetaTrader5`, published by MetaQuotes) to talk to an **installed MT5
terminal**. There is no bridge, no DLL wrapper, no third-party gateway, and no
network client of our own: the terminal owns the connection to the broker, and
the Python package is a local API to that terminal.

Code lives in `axiom_forex/brokers/mt5/` and the opt-in boundary in
`axiom_forex/brokers/external.py`. Local settings live in
`axiom_forex/config/mt5.py`, the shared OS-vault interface in
`axiom_forex/config/vault.py`.

## What this integration is, and is not

It is:

* a real market-data source (bid/ask ticks, tick-built two-sided bars, broker
  rate history labelled bid-only);
* a real execution path for **market deals and position closes**, reachable
  only through an allowed pre-trade decision, an approved + active strategy,
  explicit local arming, and reconciliation health;
* a read model for terminal/account status, symbols, positions, orders, deals,
  and a ledger reconciliation report.

It is not:

* a token-based API. MT5 authenticates with the terminal login (server, login,
  password). **No separate API token exists, so no token field is defined, and
  the existing `MARKET_DATA_READ_ONLY` token setting is not used on this path.**
* a strategy, a broker choice, a calendar, a cost model, or an account
  declaration. Those stay explicit catalog/profile values.
* a pending-order manager. `cancel_pending_order()` exists for completeness, but
  this milestone submits market deals only.

## Safety invariants

1. **No order on connect, at start, or on a settings change.** `connect()`
   opens the terminal and runs recovery; it never calls `order_send`.
2. **Explicit local authorisation, separate from the account.** `MT5Settings.mode`
   must be `TRADING_ENABLED`, *and* `arm_trading(confirm="ENABLE LIVE TRADING")`
   must be called. The authorisation is **not bound to one account**: orders are
   routed to whichever account the MT5 terminal has selected. Arming never
   survives a restart or a disconnect.
3. **Two independent gates.** The existing hard-risk gate must have produced an
   `allowed` decision, and the intent submitted must be *field-for-field* the
   approved intent (`mt5_intent_differs_from_approved_intent` otherwise). Live
   is authorised by local MT5 settings, never by setting
   `OrderRequest.target_mode = LIVE` (the gate keeps rejecting that, unchanged).
4. **Write-ahead before send.** The durable order record is written with
   `state=SUBMITTING` *before* `order_send` is called. If the record cannot be
   written, nothing is sent.
5. **Exactly one `order_send` per approval.** Every retcode is classified
   (`axiom_forex/brokers/mt5/retcodes.py`); a code this build does not know is
   treated as an **unknown outcome**, not a failure.
6. **Unknown outcomes block everything.** `UNRESOLVED` disarms trading and keeps
   blocking new orders until `recover()` resolves it from broker history. A
   resolved record reports `resend_allowed: false`: a retry is a *new*
   gate-approved request, never a replay.
7. **Broker terms are checked, not assumed.** Symbol digits, tick size, contract
   size, minimum volume, and volume step must match the catalog, and the
   catalog's maximum may not exceed the broker's. A mismatch stops trading with
   the exact diff so the catalog can be corrected.
8. **Netting vs hedging is respected.** On a `NETTING` account an opposite-side
   order would *reduce* an existing position; it is refused unless it is an
   approved close.

## Local setup

1. **Install the MT5 terminal** from the broker of your choice and log in once
   manually. In the terminal, enable *Tools → Options → Expert Advisors →
   Allow algorithmic trading*.
2. **Install the Python package** matching the terminal build:
   `pip install MetaTrader5` (Windows, or Wine/Linux with the terminal running).
   The package is optional; importing `axiom_forex` never imports it.
3. **Create the local settings document** — for example
   `mt5_settings.local.json` in the project directory (already git-ignored):

   ```json
   {
     "terminal_path": "C:/Program Files/Broker/terminal64.exe",
     "server": "Broker-Demo",
     "account_login": 12345678,
     "mode": "OBSERVE_ONLY",
     "symbol_suffix": ".pro"
   }
   ```

   `mode` starts at `OBSERVE_ONLY`. `use_existing_session` stays `true` so the
   already logged-in terminal is used without a login or a password. A document
   containing any key that looks like a credential (`password`, `token`,
   `secret`, …) is **refused**, not read, and `market_access_token_required` may
   not be set — MT5 has no separate API token.

4. **Store the password in the OS credential vault.** It is prompted, never
   echoed, never an argument, never an environment variable:

   ```
   python -m axiom_forex.cli mt5 --mt5-action set-password
   python -m axiom_forex.cli mt5 --mt5-action status
   python -m axiom_forex.cli mt5 --mt5-action clear-password
   ```

   The vault entry is bound to `server` **and** `login`
   (`terminal_login:<server>:<login>`), so a password saved for one account can
   never be reused for another. If no OS keyring backend is available the save is
   **refused** — there is no plaintext fallback anywhere in this repository.

5. **Check the connection and terms** (still no order). Build the adapter with
   the injected module and call `connect()`, then read `status()`,
   `cross_check(spec)` and `broker_terms(spec)`:

   ```python
   from axiom_forex.brokers.mt5 import import_mt5, make_mt5_adapter, make_password_store
   from axiom_forex.config.mt5 import load_settings_file

   settings = load_settings_file("mt5_settings.local.json")
   adapter = make_mt5_adapter(
       settings=settings,
       password_store=make_password_store(settings),
       repository=repository,          # FX repository for the write-ahead journal
       audit_log=audit_log,
       module=import_mt5(),
       lifecycle_lookup=lambda strategy_id: platform.lifecycle(strategy_id),
   )
   adapter.connect()                   # no order is sent here
   print(adapter.status())
   print(adapter.cross_check(spec))    # catalog vs broker terms
   ```

6. **Review `broker_terms(spec)`** and commit the values into the versioned
   instrument catalog with your own `spec_version`. Nothing is applied
   automatically.

7. **Only then** set `"mode": "TRADING_ENABLED"` and arm in-process:

   ```python
   adapter.arm_trading(
       account_login=settings.account_login,
       server=settings.server,
       confirm=f"ENABLE LIVE TRADING {settings.account_login}",
   )
   ```

## Account selection and account mode

**You choose the account in the MT5 terminal.** AXIOM reads it, never switches it,
and never logs in by itself.

* `use_existing_session: true` (default) makes `initialize()` receive only the
  terminal path and a timeout: no login, no password, no vault read. A terminal
  that is not logged in is reported as `mt5_terminal_not_logged_in`, not logged
  in automatically.
* **Account mode is information, not authorisation.** `DEMO` / `CONTEST` /
  `REAL` / `UNCONFIRMED` comes from the terminal's `trade_mode`, is shown in
  status/CLI/reports, and is audited on every connect and switch. A `REAL`
  account is never rejected for being real, never forces `OBSERVE_ONLY`, and
  never causes a disarm. `UNCONFIRMED` is reported, not guessed.
* **Trading authorisation is separate**: `mode: TRADING_ENABLED` in the settings
  plus an explicit `arm_trading()` call. Either can be absent while an account is
  selected; neither depends on demo/real.
* **A switch is expected behaviour, and is handled, not punished.** When the
  terminal account changes (`refresh_account()`, and automatically before every
  submission and at every recovery):
  * the change is audited as `MT5_ACCOUNT_CHANGED` (mode, server, account
    fingerprint, `trading_authorisation: kept`);
  * the previous account's cached state is dropped — symbol resolution, account
    validation, and the recovery view;
  * the **new** account's own currency, leverage, margin/position accounting,
    symbols, and execution terms are revalidated
    (`MT5_ACCOUNT_TERMS_VALIDATED`) before any order is routed;
  * the account is not returned to `OBSERVE_ONLY` merely because it changed.
* **No state crosses accounts.** MT5 order records carry the account
  fingerprint; unresolved-order blocking, restart recovery, and position
  reconciliation only consider the account selected right now. Another
  account's unresolved order does not block this one, and its ledger rows
  (`account_id`) are excluded from reconciliation. Positions, reservations, and
  risk state must be created per `account_id` — never reused across accounts.
* **A switch is not a trading signal.** Every submission still requires a
  qualified signal, an approved + active strategy, the existing hard-risk
  decision, the account's own validated terms, broker symbol terms, and no
  unresolved order.

Optional declarations make a mismatch loud instead of silent:

```json
{
  "declared_account_currency": "USD",
  "declared_leverage": 100
}
```

When present they must equal the selected account's values
(`account_currency_mismatch`, `account_leverage_mismatch`); when absent the
terminal's values are used as-is. A deployment can also pass
`account_profile_check=` to the adapter to validate its `AccountProfile`, risk
limits, and conversion rates for that account; the returned problems are
reported verbatim and block submission.

## Bounded session cycle

`axiom_forex/brokers/mt5/session.py` runs **one** bounded cycle and stops:

```
python -m axiom_forex.cli mt5-session --mt5-max-orders 1 --mt5-duration-seconds 30
```

The cycle connects to the terminal session the operator already opened, shows
and audits the account mode, revalidates the selected account's terms, then
checks settings validity, declared `TRADING_ENABLED` mode, instrument, approved +
active strategy, broker terms, unresolved orders for this account, arming, one
signal, the existing hard-risk decision, and finally `order_check` → write-ahead
→ `order_send`. It disarms and disconnects before returning.

`--mt5-account-mode-policy` defaults to `SELECTED_ACCOUNT` (demo or real, same
adapter and workflow). `DEMO_ONLY` is an explicit opt-in for a run that must
never touch a real account; it still connects, reports the detected mode, and
names the reason.

It sends nothing when configuration is missing, and it names exactly what was
missing. In this repository the fixture catalog is synthetic and no live
strategy or account profile is approved, so a run against a real terminal reports
`no_instrument_configured`, `no_approved_strategy_configured`,
`no_pretrade_gate_configured` (plus the request/context/signal factories) and
stays observe-only. A deployment with a real catalog, an account profile, and an
approved strategy injects them through `MT5SessionCycle(spec=..., strategy_id=...,
gate=..., request_factory=..., context_factory=..., signal_source=...)`.

**Redaction:** session reports, CLI output, and `redacted_account()` show
`login: "<redacted>"` plus a 12-character non-reversible fingerprint, the server,
mode, currency, leverage and margin mode. Balances, equity, margin, free margin,
and profit are never reported (`"withheld"`), and no password or token appears in
any surface.

## Broker-specific information still required

The terminal supplies these, but AXIOM needs them captured and reviewed before
its risk, cost, and calendar arithmetic can be trusted. Each maps to an open
decision in `axiom_forex/dependencies.py`:

| Information | Where it comes from | What AXIOM still needs |
| --- | --- | --- |
| Symbol suffix and naming (`EURUSD.pro`, `EURUSDm`, …) | `symbols_get()` | explicit `provider_aliases`/suffix in the catalog |
| Contract terms (digits, tick size, contract size, min/step/max volume) | `symbol_info()` | a versioned catalog entry that **matches**; mismatches block trading |
| Account currency | `account_info().currency` | the declared `AccountProfile` currency, rates into it |
| Leverage and margin mode (netting/hedging) | `account_info()` | declared leverage (must match) and the position-accounting assumption |
| Margin requirement per order | `order_calc_margin()` | a reviewed margin policy for the risk limits |
| Trading sessions, holidays, DST, daily breaks | terminal/broker sessions | an explicit versioned calendar — the adapter reports broker-server time and never assumes 24/5 |
| Commission, spread, swaps, triple-rollover day | deals and symbol specs | a versioned cost model and rollover policy |
| Filling mode, stops/freeze levels | `symbol_info()` | review; the adapter picks only a filling mode the broker allows |
| Tick/bar history depth | `copy_ticks_range` | a declared history window; tick depth is broker- and account-dependent |
| Server time offset vs UTC | terminal | a documented offset; timestamps are recorded as broker-server-time-labelled UTC |

## Adapter coverage

| Area | Implementation | Tests |
| --- | --- | --- |
| Terminal/account status | `terminal.py::MT5Terminal.status`, `account.py::account_terms` | fake module: connect, mismatch, exchange-margin refusal, netting/hedging |
| Symbol discovery & specs | `terminal.py::resolve_symbol`, `symbols.py::cross_check` | alias / canonical / suffix / unresolved; precision, tick, contract, volume, trade-mode diffs |
| Bid/ask ticks and bars | `terminal.py::quote/ticks/rates`, `aggregate_ticks_to_bars` | two-sided bars, incomplete last bucket, gaps omitted, bid-only rates labelled |
| Account currency & margin | `account.py::margin_for_order`, preflight currency/margin checks | mismatch and insufficient-margin refusals |
| Open positions | `orders.py::open_positions` | parsing, netting conflicts, close-volume checks |
| Order check | `orders.py::check_order` | refusal sends nothing and records nothing |
| Order submission | `orders.py::send_order`, `adapter.py::submit` | write-ahead ordering, one send, partial fill, done-without-volume, exception, `None` |
| Retcodes | `retcodes.py` | full classification table incl. unknown codes and missing results |
| Order/deal history | `reconcile.py::history_orders/history_deals` | windowed reads and matching |
| Reconciliation | `reconcile.py::reconcile_positions` | matched, ledger-only, broker-only, volume, side |
| Restart recovery | `reconcile.py::reconcile_record`, `adapter.py::recover` | deal evidence, order-history states, open position, absence-of-evidence, history unavailable, paper records ignored |
| Settings & credentials | `config/mt5.py`, `config/vault.py` | vault-only storage, refusal without a vault, mask stability, file secret rejection, CLI prompts |
| Boundaries | `brokers/external.py` | disabled by default, still refuses a bare intent when attached |
| Session cycle | `session.py` (`MT5SessionCycle`, `SessionPolicy`) | selected-account default, explicit `DEMO_ONLY` opt-in, unconfirmed mode reported, missing-configuration blockers, qualified-signal order, rejected decision, unknown-outcome disarm, time budget, mid-run switch with revalidation, redaction, CLI |
| Account scope | `adapter.py::validate_account_terms/account_state` | per-account revalidation, declared currency/leverage enforcement, profile-check items, account-scoped unresolved orders and reconciliation, audited switch that keeps authorisation |

## What cannot be verified without your MT5 account

Everything in `axiom_forex/brokers/mt5/conformance.py::BROKER_DEPENDENT_REQUIREMENTS`,
concretely:

* the real symbol list, suffixes, and contract terms of your broker;
* your account's currency, leverage, and netting/hedging mode, and the broker's
  real margin for a given volume;
* swap, commission, and triple-rollover behaviour (including when the broker
  applies three days);
* session times, holidays, DST handling, and weekend gaps for your instruments;
* actual `INVALID_FILL`/`REQUOTE` behaviour and your broker's accepted filling
  mode;
* partial-fill frequency, and how long your terminal keeps tick/deal history;
* the login handshake with a real terminal (the tests use a fake module by
  design, so no real terminal, account, or credential was touched).

Until those are confirmed on your own terminal, treat live numbers as
**unverified**, and keep `mode` at `OBSERVE_ONLY`.

## Known limitations

* Bars are built from ticks because MT5 rate bars carry one price series; a
  two-sided bar cannot be derived from a bid-only series without inventing the
  ask. Depth is therefore bounded by the terminal's tick history.
* Timestamps are the broker server's clock, labelled as such
  (`time_basis = broker_server_utc_label`). A server-time offset must be
  documented per broker; it is never silently shifted.
* `POSITION_ACCOUNTING = EXCHANGE` accounts are refused rather than priced with
  retail margin arithmetic.
* Close-by-position is supported for both accounting modes, but a partial close
  on a netting account is still submitted as a reducing deal — the broker's own
  volume rules decide, and a rejection is reported, never reinterpreted.

## Tests

```
python -m pytest tests/test_mt5_settings.py tests/test_mt5_adapter.py -q
python -m pytest tests -q          # full offline suite
```

Both suites inject `tests/fake_mt5.py`; no test imports the real
`MetaTrader5` package, connects to a terminal, reads a credential, or submits an
order.
