# AXIOM-FOREX

Forex research and paper-trading adaptation based on AXIOM's modular architecture.

**Current status:** repository-only, research/paper mode. Live trading is disabled in the new code; no real-money broker, data provider, account, credential, or running-system integration is enabled. Historical or simulated results are not evidence of profitability or live-trading readiness.

- [Architecture assessment and staged plan](docs/ARCHITECTURE_AND_PLAN.md)
- [Forex adaptation guide](docs/FOREX_ADAPTATION.md)
- [Provider and broker conformance contract](docs/PROVIDER_CONFORMANCE.md)
- [Provider profile onboarding](docs/PROVIDER_PROFILE_ONBOARDING.md)
- [Historical data import](docs/HISTORICAL_DATA_IMPORT.md)
- [Multi-pair research evaluation](docs/RESEARCH_EVALUATION.md)
- [Running-system boundary](docs/RUNNING_SYSTEM_BOUNDARY.md)

## What is implemented

The isolated `axiom_forex/` package adapts AXIOM's layers with provider-neutral interfaces and local synthetic fixtures: data normalization and calendar-aware quality, versioned instruments, indicators/strategies/regimes, chronological validation/walk-forward, bid/ask next-bar backtesting, explicit costs/rollover/margin/currency conversion, correlated-pair risk, idempotent local paper execution, SQLite persistence/migrations, monitoring, read-only WSGI API, HTML dashboard, desktop view models, and Markdown reporting.

A consolidated offline paper workflow now loads normalized bid/ask fixtures, qualifies and routes a fixture strategy signal, applies the M1 gate once, fills only through `LocalPaperBroker`, persists orders/fills/positions/audit decisions/portfolio snapshots, recovers unresolved orders on restart, and exposes one immutable workflow snapshot through `/health`, `/status`, `/workflow`, the dashboard, the report, and desktop view models. See [the integration section](docs/FOREX_ADAPTATION.md#integrated-offline-paper-workflow) for the canonical module path and remaining limitations.

A separate [provider/broker conformance layer](docs/PROVIDER_CONFORMANCE.md) defines versioned market-data and broker capability contracts, replays deterministic forex edge cases with offline fake adapters, fails closed on missing or inconsistent terms, and blocks the paper workflow before any order when its report fails. `/conformance`, `status`, health, verify, dashboard, report, desktop, and CLI expose the same report. It does not implement or enable a real provider or broker adapter.

[Provider profile onboarding](docs/PROVIDER_PROFILE_ONBOARDING.md) adds a versioned JSON profile format with provenance and effective dates for all 37 required values. It includes a synthetic fixture profile, an empty onboarding template, strict field-level validation, compatibility checks, and a safe operator report with no term values or credentials. Only the explicitly synthetic fixture profile can activate the offline paper workflow; demo and production profiles are review artifacts and never enable external execution.

An offline [historical data importer](docs/HISTORICAL_DATA_IMPORT.md) accepts one documented, provider-neutral canonical CSV of timestamped bid/ask quotes and completed bid/ask bars. It rejects unknown pairs, invalid or ambiguous timestamps (including DST wall-time ambiguity), crossed or stale quotes, incomplete bars, conflicting duplicates, records outside declared validity bounds, and imports that contradict the provider profile, pair spec, or calendar. Imported datasets carry an explicit `USER_SUPPLIED_*` data class, file and normalized hashes, time coverage, timezone basis, source provenance, and validation results in a dataset manifest. Gaps and corrections remain explicit quality events, missing prices are never interpolated, and imported data is research/backtest-only: it is never used for paper fills and is never relabelled as a synthetic fixture or production feed.

A [multi-pair research evaluation](docs/RESEARCH_EVALUATION.md) now runs on a shared multi-currency book over canonical imported datasets. It aligns several pairs and timeframes on one UTC grid, records each pair's calendar closures and missing intervals explicitly, applies the declared costs and currency conversion, and aggregates exposure per shared currency instead of treating pairs as independent. It separates chronological train/validation/test windows, keeps walk-forward folds inside the development segment, refuses to reuse a consumed test window, prices fills only from the next completed same-pair bar, and gates qualification on data class. Reports break performance and risk down by pair, session, regime, and portfolio, label every result with its dataset class and provenance, and state that no result is a profitability or live-readiness claim. Synthetic fixtures, demo-only data, mixed classes, short or incomplete series, out-of-session bars, and missing calendar or correlation terms block the run instead of producing a misleading one. The workflow creates no order and never touches the paper order stream.

A deterministic [forex stress and sensitivity suite](docs/RESEARCH_EVALUATION.md#stress-and-sensitivity-suite) reruns that same pinned evaluation under explicitly configured scenarios: wider spreads, extra slippage, commission changes, adverse swaps, delayed or missing quotes, weekend-shaped gaps, conversion-rate shocks, and increased shared-currency correlations. Every scenario is an operator-supplied assumption labeled `HYPOTHETICAL_ASSUMPTION`, `IMPORTED_DATA_BACKED`, or `DOCUMENTED_PROVIDER_TERMS` (the latter two require a reference); no "realistic" shock size is invented, fixture values are never presented as provider facts, and strategy parameters, quantities, and the split are unchanged. Comparisons are reported per pair and per portfolio across costs, drawdown, margin, exposure, coverage, and fail-closed blocks; scenarios that remove data block instead of interpolating. Stress runs claim no out-of-sample window, are forced to `SENSITIVITY_ONLY`, and can never qualify or activate a strategy.

Verify the repository locally without external services:

```bash
python -m pytest -q
python -m pytest -q tests/test_forex_conformance.py
python -m pytest -q tests/test_forex_provider_profiles.py
python -m pytest -q tests/test_forex_data_import.py
python -m axiom_forex conformance
python -m axiom_forex profile
python -m axiom_forex verify
python -m axiom_forex workflow
python -m axiom_forex report
```

Import the offline format example (illustrative values only):

```bash
python -m axiom_forex import \
  --import-path imports/SAMPLE_canonical_quote_bars.csv \
  --import-data-class USER_SUPPLIED_DEMO \
  --import-source SAMPLE_CANONICAL_CSV --import-source-version sample-v1 \
  --import-valid-from 2026-03-10T13:00:00Z \
  --import-valid-until 2026-03-10T14:00:00Z
```

The `app/` package remains the strict M1 fail-closed safety/domain kernel. `axiom_forex/` composes those safety primitives with the rest of the architecture while keeping every external integration disabled.

## MetaTrader 5

[MetaTrader 5 integration](docs/MT5_INTEGRATION.md) replaces the generic external-broker placeholder with an adapter over the official `MetaTrader5` Python package and an installed terminal: terminal/account status, symbol discovery and broker-term cross-checks, bid/ask ticks and tick-built two-sided bars, account currency/leverage/margin reads, positions, order checks, market-deal submission, order/deal history, reconciliation, and restart recovery. The only secret is the terminal password, stored in the operating-system credential vault and entered/replaced/cleared locally with `python -m axiom_forex.cli mt5 --mt5-action set-password`; no token field exists because MT5 has no separate API token. Live submission is off by default, requires an explicit local `TRADING_ENABLED` mode plus an in-process `arm_trading()` call naming the account, an allowed pre-trade decision, and an approved active strategy; nothing is sent on connect or at start, the durable order record is written before `order_send`, and an unknown outcome disarms trading and blocks further orders until reconciliation resolves it. A bounded session cycle (`python -m axiom_forex.cli mt5-session`) connects to the already-running terminal, detects and audits the account mode, and runs at most one gate-approved order. You choose the account in MT5: the default `SELECTED_ACCOUNT` policy routes to whichever account the terminal has selected, demo or real, with the same adapter and workflow (`--mt5-account-mode-policy DEMO_ONLY` is an explicit opt-in for a demo-only run). Account mode is reported and audited but never substitutes for the separate trading-enabled setting; a terminal account switch keeps that setting, drops the previous account's cached state, and revalidates the new account's currency, leverage, margin mode, symbols, and execution terms before routing. Order records and reconciliation are account-scoped, so no account's positions, reservations, risk state, or unresolved orders are reused by another. Reports redact the account identifier and never show balances, equity, or credentials. All tests use a fake `MetaTrader5` module: no real terminal, account, or credential is touched.

## Windows workstation (observe-only UI)

`axiom_forex/workstation/` is a local, read-only interface over the paper platform and an
already-signed-in MetaTrader 5 terminal. It exists so you can see what AXIOM reads without any
way to trade from the interface: no order entry, no arming, no cancel/modify, no account
switching, no broker-settings changes, no automatic reconnection, and no background polling.

### One-time setup on Windows

1. Install **Python 3.11 or newer** (tick *Add python.exe to PATH* in the installer), then in a
   new terminal:

   ```bat
   py -m pip install -e .
   py -m pip install MetaTrader5
   ```

2. Install MetaTrader 5 **from your broker**, open it once, and sign in to the account you want
   to observe — demo or real, your choice. Leave the terminal running and signed in. AXIOM
   reuses that session: it never asks for, stores, or displays your MT5 password, and it never
   logs in for you.

3. Create `mt5_settings.local.json` in the repository root (already git-ignored). It holds no
   credential — only which terminal to attach to:

   ```json
   {
     "terminal_path": "C:/Program Files/MetaTrader 5/terminal64.exe",
     "server": "<your broker server, e.g. Broker-Demo>",
     "account_login": <your account number>,
     "mode": "OBSERVE_ONLY",
     "use_existing_session": true
   }
   ```

   The setup screen shows this template, and reports the terminal paths and packages it
   actually found on your machine. The path lives in this file, not in the interface.

### Launch

Double-click **`scripts\start-workstation.cmd`**, or run one of:

```bat
py -m axiom_forex.workstation.launch                    :: connect to your local terminal
py -m axiom_forex.workstation.launch --simulate         :: offline preview, sample data only
py -m axiom_forex.workstation.launch --print-prerequisites
py -m axiom_forex.workstation.launch --port 8766        :: if 8765 is taken
py -m axiom_forex.workstation.launch --no-browser       :: do not open a browser
```

The launcher opens `http://127.0.0.1:8765/` in your browser once the server is listening
(`--no-browser` suppresses that). It binds to loopback only. The launcher refuses to
expose a real terminal session on any other interface, and on a machine without Windows or
MetaTrader 5 it stops and prints exactly what is missing instead of pretending to connect.

### What the two modes mean

| Launch | Session panel says | Can reach "ready for observation"? |
| --- | --- | --- |
| default | `Connected · <mode>` after **Connect** | Yes, after you review and confirm |
| `--simulate` | `Simulator · not MT5`, banner **SIMULATED / SAMPLE DATA** | Never |

The simulator starts **disconnected and unconfirmed**. Pressing its button starts a preview of
the interface with sample values; it contacts nothing, every value it shows is labelled `Sample`
or `SAMPLE DATA`, and the wizard's ready step stays blocked. Only a successful, explicit
**Connect** to your real local terminal — the one you signed in to — can produce *ready for
observation*, because only then has AXIOM actually read an account.

### First run with a real terminal

1. **Check this machine** — Windows, Python ≥ 3.11, the `MetaTrader5` package, the terminal, and
   your settings file are detected; anything missing is listed with the exact command to fix it.
2. **Sign in inside MetaTrader 5** — you do this in the terminal, then tick the confirmation.
   AXIOM never asks for the password.
3. **Connect (observe only)** — an explicit click. It attaches to the running terminal, reads
   status, account terms, and Market Watch symbols, and gets no order capability of any kind.
4. **Review the detected account** — broker/server, currency, leverage, margin accounting,
   account mode (demo or real, as *the terminal* reports it), terminal trading permission, and
   how many symbols are available. The login appears only as a short fingerprint; balances and
   equity are never read into the interface.
5. **Confirm account and symbols** — choose the symbols to observe, then confirm. Nothing is
   inferred: what you confirm is what the terminal reported.
6. **Ready for observation** — shown only for a real terminal, with what was *not* enabled
   listed explicitly (no trading, no order entry, no arming, no auto-reconnect).

If MetaTrader 5 is closed, not signed in, or the `MetaTrader5` package is missing, **Connect**
fails and says why; nothing retries on its own, and pressing Disconnect stops the session until
you press Connect again.

### What you are looking at

The persistent status bar shows connection state and the account mode together, so demo and
real are never confused. Every figure carries its data class — `REAL BROKER`, `PAPER`,
`SAMPLE FIXTURE`, or `SIMULATED` — and the paper portfolio, positions, and audit activity come
from the offline paper platform, not from your broker. Session-time values come from the
declared calendar and are marked indicative until your broker's sessions, holidays, and DST
rules are confirmed.

The interface contains exactly six actions, all read-only setup: `connect`, `disconnect`,
`acknowledge-sign-in`, `confirm`, `unconfirm`, and `symbols`. There is no endpoint that can
trade, arm trading, switch accounts, or change broker settings.

## Safety

This repository must not contain credentials, secrets, or a live order-routing path. Unknown instruments, stale or incomplete quotes, missing specifications, closed markets, invalid risk calculations, unresolved orders, failed provider/broker conformance, failed provider profile onboarding, rejected or quarantined imported data, or live-mode requests fail closed and block new orders. Generic 24/5 availability is never a production default; explicit broker calendars, daily breaks, holidays, DST, weekend gaps, instrument availability, costs, financing, leverage, and account terms must be supplied and conformance-tested before any external integration is considered.
