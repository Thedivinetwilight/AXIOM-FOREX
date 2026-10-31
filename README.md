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

## Windows workstation (paper and live execution)

`axiom_forex/workstation/` is a local interface over the paper platform and an
already-signed-in MetaTrader 5 terminal. It owns exactly one trading-shaped control — the
**execution mode** — and nothing else: no order entry form, no arming control, no
cancel/modify, no account switching, no broker-settings changes, no automatic reconnection,
and no background polling.

| Mode | What it does |
| --- | --- |
| **Paper trading — live market data** | Reads bid/ask ticks from the terminal you connected and simulates every fill locally through the paper engine. No order is ever sent. Active by default. |
| **Live trading — MetaTrader 5 execution** | Routes an approved, gate-passed order through the existing MT5 adapter to the account you signed in to. Selected when you confirm once, naming the account. |

Execution mode and account type are different things, and the interface never conflates
them. The **mode** (paper/live) is your decision and is remembered in
`execution_mode.local.json` beside your settings file. The **account type** (demo/real) and the
**server** are read from the terminal and shown prominently — AXIOM never switches MT5
accounts, never logs in for you, and never picks a broker. If you want a different account,
sign in to it inside MetaTrader 5 and press Disconnect, then Connect.

### Switching mode — the exact flow

1. Open **Trading mode** in the left navigation (or press *Trading mode* on the
   dashboard). The PAPER/LIVE indicator is in the top bar and the sidebar on **every** page.
2. **Both modes can be selected at any time.** No preflight, no cooldown, no completed paper
   test, and no strategy qualification stands between you and either choice: a check that is
   not met refuses the *order* it applies to, with its reason shown, and never takes the
   switch away or greys it out.
3. **To paper:** press *Switch to paper now*. It is immediate — no confirmation, because it can
   only remove execution capability. It stops new live entries at once and **closes nothing**:
   broker positions stay open, are listed on the page, and are yours to manage in the terminal.
4. **To live:** the page states plainly that real-money orders may follow from the active
   strategy, then asks for two things — type `ENABLE LIVE TRADING`, and type the signature of
   the account connected right now. That is the only confirmation, it is recorded, and it sends
   nothing: no order, no connection, no arming. LIVE then means LIVE-against-that-account; if
   the terminal is later signed in elsewhere, orders are refused rather than rerouted.
5. Switching back to paper is always one click. Disconnecting, or restarting the workstation,
   also returns to paper: a saved `LIVE` is remembered but is **never** effective in a new
   process until you confirm the account again.

Every switch and every refused switch is appended to `execution_mode_audit.local.json`
(also beside your settings file) and to the platform's durable audit log when one is open. The
records contain mode names, reason codes, and timestamps — never a credential, an account
number, a balance, or an equity figure.

Both files are `*.local.json`, so they are git-ignored and travel only with your machine.

### Why an order may still be refused

Every check is listed on the page with the step that clears it, and none of them touches the
mode switch. Two commonly need real work:

* `mt5_account_mismatch` / `mt5_server_mismatch` — MetaTrader 5 is signed in to a different
  account or server than `mt5_settings.local.json` names. Sign in to the configured account
  inside the terminal, then press Disconnect and Connect again. The identity check is **not**
  relaxed: a different account is never treated as a match, and it is not the account the
  session confirmed either, so the order is refused on both counts.
* `strategy_not_approved` — if a deployment attaches a strategy that is not approved and
  active, no order is submitted through it. This repository ships **no** approved live strategy
  and AXIOM never invents one, so live mode can be selected here but cannot produce an order
  until a deployment attaches its own approved strategy through
  `MT5SessionCycle`/`MT5TradingAdapter`.

### One-time setup on Windows

1. Install **Python 3.11 or newer** (tick *Add python.exe to PATH* in the installer), then in a
   new terminal:

   ```bat
   py -m pip install -e .
   py -m pip install MetaTrader5
   ```

2. Install MetaTrader 5 **from your broker**, open it once, and sign in to the account you
   want to use — demo or real, your choice. Leave the terminal running and signed in. AXIOM
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

   Live execution also needs `"mode": "TRADING_ENABLED"` in this same file — an explicit local
   opt-in, separate from the paper/live switch in the browser. Leaving it `OBSERVE_ONLY` keeps
   live mode blocked, with that reason listed on the Trading mode page.

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

### Updating an existing clone

Close the running workstation first (Ctrl+C in its console), then double-click
**`scripts\update-workstation.cmd`** — or run it from a terminal in your clone:

```bat
scripts\update-workstation.cmd
```

It updates the branch you already have, in place:

```bat
git fetch origin arena/f67c2859-axiom-forex
git merge --ff-only origin/arena/f67c2859-axiom-forex
.venv\Scripts\python.exe -m pip install -e . --no-deps
```

It never reclones, resets, cleans, stashes, or overwrites anything, and it leaves
`mt5_settings.local.json`, `.venv`, and every other untracked or ignored file exactly as
it is. It stops **before changing anything** — and says why — when Git is missing, the folder
is not a Git working tree, the checkout is on another branch (or detached), a *tracked* file
has local changes, or the branch cannot fast-forward. Your local edits are never discarded for
you: commit or revert them yourself, then re-run the updater.

### What the two launches mean

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
3. **Connect to the terminal** — an explicit click. It attaches to the running terminal and
   reads status, account terms, Market Watch symbols, and the live ticks paper mode is priced
   from. Connecting never enables trading; the execution mode is a separate decision.
4. **Review the detected account** — broker/server, currency, leverage, margin accounting,
   account mode (demo or real, as *the terminal* reports it), terminal trading permission, and
   how many symbols are available. The login appears only as a short fingerprint; balances and
   equity are never read into the interface.
5. **Confirm account and symbols** — choose the symbols to observe, then confirm. Nothing is
   inferred: what you confirm is what the terminal reported. The list is built from the
   terminal's own Market Watch, and only a symbol it describes with both currency legs is
   selectable; metals, stock CFDs, and anything whose legs were not reported are listed with a
   short reason instead of being guessed at from the ticker. **Select all verified forex
   pairs** takes exactly the verified pairs the broker reports as tradable, **Clear selection**
   empties the set, and the page always shows how many are selected. Broker names are kept
   exactly, suffixes included (`EURUSD.a` stays `EURUSD.a`); no name is substituted or
   canonicalised. Any change to the selection withdraws the confirmation until you confirm
   again.
6. **Ready for observation** — shown only for a real terminal, with what was *not* enabled
   listed explicitly (no order entry, no arming, no auto-reconnect) and a pointer to the
   Trading mode page where paper or live is chosen.

If MetaTrader 5 is closed, not signed in, or the `MetaTrader5` package is missing, **Connect**
fails and says why; nothing retries on its own, and pressing Disconnect stops the session until
you press Connect again.

### What you are looking at

The persistent status bar shows the execution mode (`PAPER · simulated fills`,
`LIVE · MT5 execution`, or `LIVE · not confirmed`), the connection state, and the account mode
together, so paper/live and demo/real are never confused. The mode banner also states where
prices come from: terminal ticks, or none. Every figure carries its data class — `REAL BROKER`, `PAPER`,
`SAMPLE FIXTURE`, or `SIMULATED` — and the paper portfolio, positions, and audit activity come
from the offline paper platform, not from your broker. Session-time values come from the
declared calendar and are marked indicative until your broker's sessions, holidays, and DST
rules are confirmed.

### Start here: the guided paper session

The home page (`/`) is **Start paper trading**: one summary, five steps, one obvious next
action. Work down the list and the page moves with you.

| Step | What it does | The button |
| --- | --- | --- |
| 1 | Connects to the MetaTrader 5 terminal you already have open and signed in | **Connect (read-only)** |
| 2 | Chooses the forex symbols, verified from the terminal's own currency legs | **Select all verified forex pairs** (or *Choose individually*) |
| 3 | Reads the terminal's own bars for those symbols and shows what it got, with a reason for anything unusable | the check runs on page load; **Re-check** refreshes it |
| 4 | Evaluates the declared strategy catalog on that history | **Evaluate the strategies** |
| 5 | Starts the live-market paper session on the screened rows you tick | **Start PAPER trading** |

Above the steps, one status block states the connection, the paper session (not started,
running, stopped or blocked), the selected symbols and strategy, the newest market data and
whether it is fresh, the last signal, the last simulated order or fill, the open paper
positions, and the exact reason trading has not started. The highlighted step's button is the
primary action, and the same button sits in the top block.

Everything else — Account & symbols, Data & evaluation, Strategy library, Quick setup,
broker terms, the trading mode page, the LIVE workflow, Kronos, System health — is under a
collapsed **Advanced** disclosure and in the sidebar's *Advanced* group. None of it is needed
for the five steps above.

The guided page adds no new kind of action: it posts `connect`, `symbols`,
`catalog-evaluate`, `catalog-paper-start` and `catalog-paper-stop`, all of which already
existed and all of which refuse without the page's per-process nonce. The whole set of state
changes is declared in `POST_ACTIONS` in `axiom_forex/workstation/app.py` and pinned by the
test suite. There is no endpoint that can place an order, arm trading, switch accounts, or
change broker settings; the mode actions never connect, submit, or arm anything, the paper
actions only simulate fills, and there is no JSON API.

## Paper strategy runner (real-time, paper fills only)

`axiom_forex/paper_runner/` runs one documented candidate strategy against completed bars and
sends every signal through the existing planner, pre-trade gate, paper engine, reservations,
settlement, and ledger. It cannot call `order_send`, cannot arm live execution, and cannot change
the execution mode: it refuses to run unless the mode is `PAPER`. Full detail, including every
reason code and every fail-closed path: [docs/PAPER_RUNNER.md](docs/PAPER_RUNNER.md).

Three preconditions, in order, because a strategy is a hypothesis until the data says otherwise:

1. **Audit what history you have.** `python -m axiom_forex.cli data-audit --evaluate-markdown`
   lists the canonical CSVs in `imports/`, flags anything that is not canonical or is only a
   format example, and lists what has actually been imported. Nothing is downloaded or guessed.
   A fresh checkout ships a 6-bar **format example**, not market data.
2. **Evaluate once, on your own licensed history.** `python -m axiom_forex.cli paper-evaluate
   --import-path imports/YOUR_EXPORT.csv --import-data-class USER_SUPPLIED_HISTORICAL ...`
   splits the sample chronologically (60/20/20), runs the fixed candidate grid plus baselines,
   and spends the **locked test window once per dataset**. Costs are the spread in your data plus
   the platform's commission, slippage, and swap model; fills are the next bar's executable side.
   Predeclared criteria (minimum 30 closed test trades, 10% drawdown cap, positive validation and
   test net P&L after costs, at least 3 walk-forward folds with 60% positive, no gate rejections,
   usable data quality, promoting data class) decide whether a candidate is `SELECTED_FOR_PAPER`,
   `REJECTED`, or `INSUFFICIENT_EVIDENCE`. **Demo and fixture data can never select a strategy.**
3. **Run it.** `python -m axiom_forex.workstation.launch --paper-runner
   --paper-runner-source terminal` (or `--simulate --paper-runner --paper-runner-source replay`
   for an offline, clearly labelled replay). The *Paper strategy runner* panel on Overview and
   Positions shows running/stopped, the last tick, signal, and fill times, the selected strategy
   and its parameters, the feed's data class, the counters, and a reason for every quiet cycle.
   **Start runner** begins cycles; **Stop runner** (or Ctrl+C) ends them. Restarting with the same
   `--paper-runner-database` restores the ledger and counters and then waits for an explicit Start.

### Live execution

`/live` is the only screen from which a real order can ever be sent, and it says
in one line whether the next strategy signal would send one. Enabling LIVE, arming
the plan, and starting the session are three separate clicks; none of them sends
an order, and **Stop and disarm** always works.

You choose the strategy, symbol, order size, maximum open positions, and daily
loss limit, then read a review of exactly what arming authorises before typing the
confirmation phrase. A strategy without measured results is labelled
**UNVALIDATED** — AXIOM never picks one for you and never presents it as tested.
Every order still goes through the same planner, pre-trade gate, risk limits and
ledger the paper lane uses; the intent is written durably before submission, and
an in-doubt order is never re-sent. Full detail: `docs/LIVE_EXECUTION.md`.

### Strategy library (optional, `/strategies`)

`/strategies` is a research index of **documented** strategy templates — moving-average
crossover, time-series momentum, Donchian breakout, Bollinger reversion, ATR-scaled
breakout, session range — each with its source, its rules, its parameters, the
timeframes it is explicitly analysed on, its assumptions, and its licence. Templates
that need data AXIOM does not import (historical swap rates, multi-pair legs) are
listed as **not evaluable**, with the missing input named, instead of approximated.

"Large" means variants of those templates, counted honestly: the page shows templates,
evaluable templates, predeclared variants, and **attempts recorded** as four separate
numbers, and every attempt — including every failure — is written to a local ledger.
Screening runs on the same chronological evaluator, fills, costs, splits and
walk-forward folds the paper lane uses, applies a bootstrap p-value,
Benjamini–Hochberg FDR and White's Reality Check across the whole search, and refuses
to make anything eligible on fixture or demo data.

Only an **eligible** attempt can be selected, only by you, with a confirmation, and
selecting never sizes, arms, or trades: the selected variant then appears in the live
plan, where the review, the confirmation phrase, the risk gate and the protective-stop
requirement still apply. A timeframe no template declares is never analysed. Full
detail: `docs/STRATEGY_LIBRARY.md`.

### Kronos forecasting (optional, `/kronos`)

Kronos is a published financial foundation model that predicts the next few
K-lines. AXIOM runs it as an **optional, replaceable forecasting component** on its
own page, separate from Provider setup and from Live execution. It is forecast-only:
the page cannot place an order, size a position, set a stop, arm anything, or change
an execution mode, and a forecast is labelled `FORECAST_ONLY_NOT_A_SIGNAL`
everywhere it is stored or shown.

One action installs it: **Install recommended setup** on `/kronos` puts the smallest
official variant, its official tokenizer, and the official source tree at a pinned
commit into an AXIOM-managed folder (`data/kronos/`, Git-ignored), records the paths
and revisions in `kronos_install.local.json`, and needs no path typed by you. A
check first shows the exact download size, the destination, this machine's free
space and detected hardware; nothing downloads until you press Install, nothing
downloads at startup, and the download can be cancelled and cleaned up. AXIOM never
installs software packages: missing requirements are listed with their exact lines.
The first forecast defaults to the first verified symbol from **Account & symbols**,
and the symbol, timeframe and horizon can be changed and re-run without reloading
the model. Model variants, custom paths, revisions and hardware overrides live under
a collapsed **Advanced settings** section. **Inspect and hash the model files**
prints each file's SHA-256 locally so you can compare it with the publisher's own
values — the weights' state stays `UNVERIFIED_BY_AXIOM`, because AXIOM has nothing
upstream to check them against.

Input mapping is explicit and never invented: OHLC is the **bid/ask midpoint**
(labelled `MID`, not tradable), `volume` is MT5's **tick count** (labelled
`TICK_VOLUME`, which is not exchange volume), `amount` is derived and labelled, and
a missing field is omitted rather than zero-filled by us. Every forecast is stored
with its model, revision, settings fingerprint, and input-quality report before it is
shown, then scored later against realised bars — direction, absolute error, and band
coverage, with no profit figure. A benchmark against hold/drift/momentum baselines
runs on the chronological out-of-sample window only, and **a benchmark score is
never evidence of profit**. `Kronos-large` is not offered: the project does not
publish its weights. Full detail: `docs/KRONOS.md`.

### Provider setup (`provider_setup.local.json`)

The runner trades the symbol *your broker* quotes, so it needs one local document
with your broker's terms. Build it in the browser: **Provider setup** (`/provider-setup`).

* **What AXIOM fills in for you**: the account currency and leverage, and each
  symbol's exact broker name, suffix, and tick size — all read from the connected
  terminal (`account_info()`, `symbol_info()`). Those fields are marked
  *from terminal* and cannot be typed over.
* **What you supply**: commission, slippage, financing, swap, the rollover cutoff,
  the trading calendar (session windows with validity bounds), conversion rates,
  the risk limits, the entry cap, the lot size, and a versioned label for each.
  Every one is marked *you supply* and is required. A blank commission is blank,
  never `0`; a blank risk limit is a blocker, never a default.
* **What it writes**: `provider_setup.local.json` in the project root, atomically,
  only after the document passes the *same* loader the runner uses. It is ignored
  by Git (`*.local.json`), and it is never read back into a page.
* **What it never touches**: `mt5_settings.local.json` (never read, written, or
  moved), and any credential — the loader refuses a document that contains a
  password, token, API key, or market-access-token field.
* **A safe example**: `profiles/provider_setup.example.json` documents the real
  `axiom-provider-setup-v1` schema with placeholder values and a `_status` of
  `EXAMPLE ONLY -- NOT RUNNABLE`. It is *not*
  `profiles/provider_onboarding_template.json`, which is a different schema for a
  different purpose.

After saving, the page lists what is still missing: the required broker tick-history
import, the evaluation, and the candidate selection.

### If the runner says it is blocked

The runner card never shows an exception. Pressing **Start** either starts the
lane or reports the exact blocker, in the operator's words:

| Blocker | What it means | What to do |
| --- | --- | --- |
| `terminal_not_connected` | No terminal session is open, so there are no symbols or ticks | Press **Connect** on Setup, then **Start** again |
| `market_data_unavailable` | This session has no read-only market data (a `--simulate` preview) | Start against your terminal |
| `provider_setup_missing` | Started without `--paper-runner-provider-setup`, so there are no broker terms | Add the flag, then build the document on **Provider setup** |
| `provider_setup_file_missing` | The flag points at `provider_setup.local.json` and that file does not exist | Open **Provider setup** and save it — no restart needed |
| `no_imported_history` | No evaluable dataset in the import folder | Export MT5 ticks, convert and evaluate them |
| `strategy_not_evaluated` | No candidate has passed the predeclared criteria on your data | Read the blocking criterion on Data & evaluation |
| `symbol_unresolved` | Your terminal does not quote the configured symbol | Fix the pair/suffix in the setup document; nothing is substituted |
| `runner_build_failed` | Something else went wrong while building the lane | The page says so plainly; the console has the exception type |

A fix for an earlier build: the runner used to ask the observation session for a
`terminal` attribute it does not have, which surfaced as
`AttributeError: 'ObservationSession' object has no attribute 'terminal'` on the
Positions page. The session now exposes two read-only accessors
(`market_data_source()` and `market_data(freshness)`), the runner uses those, and
a failed build is reported as a blocker.

### Importing and evaluating from the workstation itself

The same three steps are on the **Data & evaluation** page (`/data`), so you do not need a
terminal for them. Start the workstation with the runner flags (`--paper-runner
--paper-runner-provider-setup <file>` adds the page's evaluation platform), drop your MT5 tick
export into `imports/` (or point at another folder with `--imports-dir <folder>`), then:

1. **Re-scan folder.** Every file is listed with its kind, records, span, and flags. A file whose
   name starts with `SAMPLE` is marked as a sample and can never be evaluated as history.
2. **Convert** a broker tick export. You must type the pair and the *server's* UTC offset (or its
   IANA zone) — there is no default, because a guessed time basis would silently shift every bar.
   The conversion writes the canonical CSV plus a provenance sidecar next to it and reports the
   bars written, the ticks read, rejected rows, duplicates, and every empty window as a gap. It
   never fills a gap, never splits a candle, and never derives one side of the quote from the other.
3. **Evaluate** the canonical file. Declare its data class (historical / demo / production) and tick
   the declaration box; AXIOM will not guess. The same chronological split, walk-forward folds,
   locked test window, costs, and criteria as `paper-evaluate` run here, the evidence is recorded in
   the runner's own database, and the page shows each candidate's status and exactly which criterion
   blocked it. The locked window is spent once per dataset — re-running it is refused, because that
   would turn a test set into a tuning set.

Nothing on that page connects anywhere, places an order, arms anything, or changes the mode. Every
form is nonce-guarded and the page renders with no JavaScript at all.

It fails closed on live mode, an unselected candidate, an invalid instrument spec, an unresolved
order, an intent without an order, a stale tick, a data gap, a missing price, a closed market, a
gate refusal, or a lost terminal — each with its own reason code, none of which trades. The
intent is written to SQLite *before* the paper engine executes, so a crash between the two makes
the next start refuse rather than guess.

### Real broker symbols and the live path

Two additions make the runner usable on a symbol your own terminal quotes, and make live execution
a real path rather than a label:

- **The provider lane.** `--paper-runner-provider-setup ./provider_setup.local.json` builds the
  paper platform from *your* terms plus the **connected terminal's own symbol catalogue**:
  `brokers/mt5/catalogue.py` resolves the broker symbol exactly (explicit name, then
  `canonical + suffix`, then the bare six letters), refuses an ambiguity, and builds the spec from
  `symbol_info` cross-checked against your document. `config/provider_setup.py` loads the account,
  costs, swap cards, risk limits, entry cap, calendar, and execution terms, and refuses a document
  that is missing any of them or that looks like it holds a credential. Conversion rates come from
  the broker's own quotes (`paper_runner/conversion.py`), labelled `DIRECT`, `INVERSE`, or `CROSS`,
  and a leg that cannot be derived refuses the build instead of guessing.
- **Tick history.** `python -m axiom_forex.cli convert-ticks --ticks-path <MT5 tick export>
  --ticks-pair EUR/USD --ticks-server-offset-minutes <offset>` turns a real two-sided tick export
  into the canonical bid/ask CSV plus a provenance sidecar (source hash, tick counts, coverage, every
  gap, the declared server offset, `DERIVED_FROM_BROKER_TICKS`). It refuses a candle export, an
  unsorted file, a crossed quote, and an unspecified or ambiguous time basis.
- **The live leg.** `paper_runner/live.py` takes an approved plan through the *same* pre-trade gate
  the paper path uses, wraps the approved intent, and routes it through
  `ExecutionRouter.route_live` → `MT5TradingAdapter.submit` → `order_send`, books the broker's own
  fill back into the ledger the gate reads, then reconciles positions against MT5. It lists every
  blocker it can see (`mode_not_live`, `live_not_confirmed`, `live_not_armed`,
  `live_account_mismatch`, `unresolved_order_present`, `duplicate_request`,
  `live_positions_mismatch`, `live_fill_not_booked`, …), submits each plan once, and treats an
  unknown outcome as unresolved: never resent, disarmed, blocked until broker history explains it.
  Switching modes never submits anything, and the leg holds no order-building code.
- **The live entry point.** `axiom live-run` assembles the provider platform, the planner, the
  gate, the approved candidate, and the live leg in one bounded, reconciled run. It refuses unless
  the adapter is already connected *and* armed, so starting a runner can never arm a broker.
- **The operator's live workflow.** In the workstation, **Live execution** (`/live`) is the whole
  of it: choose the strategy, symbol, order size, maximum open positions and daily loss limit, read
  the review, type the confirmation phrase, arm, then start. The page states in one line whether
  the next strategy signal would send a real order, and **Stop and disarm** always works. Arming
  writes `live_plan.local.json` (Git-ignored) and sends nothing; a strategy without measured
  results is labelled **UNVALIDATED** and is never chosen for you. See `docs/LIVE_EXECUTION.md`.

It is proven against the **fake** MetaTrader 5 module only: no real order has been placed and no
account has been contacted. Broker-specific symbols, filling modes, retcodes, and margin behaviour
remain unverified until you run it on Windows with your own account.

Reported honestly: no market data is included, no candidate has been evaluated on real licensed
history here, and no profitability or live-readiness claim is made. Win rate is always reported
next to net expectancy after costs, drawdown, trade count, and losing periods — including when
they are poor.

## Safety

This repository must not contain credentials or secrets, and it ships no approved live strategy:
live order routing exists only behind an explicit mode confirmation that names the connected
account, an approved active strategy, arming, and the per-order pre-trade gate. Unknown instruments, stale or incomplete quotes, missing specifications, closed markets, invalid risk calculations, unresolved orders, failed provider/broker conformance, failed provider profile onboarding, rejected or quarantined imported data, or live-mode requests fail closed and block new orders. Generic 24/5 availability is never a production default; explicit broker calendars, daily breaks, holidays, DST, weekend gaps, instrument availability, costs, financing, leverage, and account terms must be supplied and conformance-tested before any external integration is considered.
