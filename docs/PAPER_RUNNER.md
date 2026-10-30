# Paper strategy runner

A real-time **paper** runner: completed bars in, simulated fills out, and nothing
sent to anyone. It reuses the repository's existing planner, pre-trade gate,
paper engine, reservations, settlement, recovery, and ledger. It adds a feed, a
documented candidate strategy, a write-ahead intent, and a run journal — and no
second trading stack.

**What it is not.** It cannot arm live execution and cannot change the execution
mode. In `PAPER` it sends nothing to anyone. In `LIVE` it can place a real order,
but only when a live path is wired into the process *and* every live check passes
(§6b); a runner with no live path refuses to start in `LIVE` rather than
simulating fills under a live label. It makes no profitability claim: it runs a
*hypothesis* whose results on your data are evidence, not a forecast.

---

## 1. The one rule everything else follows

A decision is made from bars that have already **closed**. The fill uses a price
that arrived **after** that close. On a terminal feed that price is the first tick
of the next window; in a replay it is the next bar's open (ask for a buy, bid for
a sell). If the only price available is stamped before the decision bar closed,
the planner refuses the order (`STALE_QUOTE` / `quote_precedes_decision`) rather
than filling at a price nobody could have traded on.

---

## 2. Why there is a data-audit and an evaluation in front of it

The runner will not start until it has three things:

1. **Imported, licensed history** — a canonical CSV you placed in `imports/` and
   imported yourself. This repository ships a *format example*, not market data,
   and nothing here downloads anything.
2. **A recorded evaluation** of the candidate on that data, with the locked test
   window spent once.
3. **PAPER mode** and a clean prior state (no unresolved order, no intent without
   an order, a valid instrument spec).

Until all three are true the runner reports `BLOCKED` with the reason. That is
the normal, honest state of a fresh checkout.

### 2.1 Audit what you have

```bash
python -m axiom_forex.cli data-audit --evaluate-markdown
```

It lists the CSVs in `imports/`, flags anything that is not canonical, flags
sample/demo files, and lists the datasets actually recorded in the paper database
with their declared data class, source, and validity bounds. It never guesses a
provenance and never reads outside the directory you name.

### 2.2 Evaluate, once, on your data

```bash
python -m axiom_forex.cli paper-evaluate \
  --database-path ./paper.local.sqlite3 \
  --import-path imports/YOUR_EXPORT.csv \
  --import-data-class USER_SUPPLIED_HISTORICAL \
  --import-source YOUR_LICENSED_SOURCE --import-source-version v1 \
  --import-valid-from 2026-01-02T00:00:00Z --import-valid-until 2026-03-31T21:00:00Z
```

The declarations are required: the importer does not infer a pair, a timeframe, a
data class, a source, or validity bounds. See
[HISTORICAL_DATA_IMPORT.md](HISTORICAL_DATA_IMPORT.md) for the exact columns and
for how to turn a MetaTrader 5 export into that format.

The command:

* splits the sample **chronologically** 60% train / 20% selection / 20% locked
  test, with no shuffling;
* runs every candidate in a fixed, predeclared grid plus two baselines on the
  same windows;
* spends the locked test window **once per dataset** and records that fact, so
  re-running the same dataset is refused (`locked_test_already_consumed`);
* writes the evidence to the paper database, where the runner reads it.

### 2.3 What the evaluation must clear

Criteria are declared in `axiom_forex/paper_runner/evaluation.py::Criteria` before
any result is seen. A candidate that misses one is **rejected**; it is not moved
to a different window and retried.

| Criterion | Default |
| --- | --- |
| Minimum closed trades in the locked test | 30 |
| Maximum drawdown as a fraction of starting equity | 10% |
| Validation net P&L after costs | must be positive |
| Locked-test net P&L after costs | must be positive |
| Walk-forward folds | at least 3 |
| Walk-forward folds that are positive | at least 60% |
| Gate rejections inside the locked test | none |
| Data quality | the run must be `usable` |
| Data class | must be `USER_SUPPLIED_HISTORICAL` or `USER_SUPPLIED_PRODUCTION` |

Costs are not estimated here. The spread is whatever your bid/ask data contains;
commission, slippage, and swap come from the platform's configured cost model and
rollover engine. Fills are the next bar's executable side, and missing bars reject
the run instead of being filled in.

**Demo and fixture data can never promote a candidate.** They can be evaluated
(sanity only) and are reported as `INSUFFICIENT EVIDENCE`, because evidence about a
fixture is not evidence about your broker's market.

A candidate is `SELECTED_FOR_PAPER` only when every criterion passed on a promoting
data class. Anything else is `REJECTED` or `INSUFFICIENT_EVIDENCE`, and the runner
will not start.

---

## 3. The candidate

`TrendPullbackV1` (`axiom_forex/paper_runner/strategy.py`) on 1-minute bars:

* trend: the 8-bar average of mid closes is above the 21-bar average;
* entry: the latest completed close dips below the fast average while staying
  above the slow one — a pullback inside an up-trend (*edge-triggered*: only on
  the bar the condition first becomes true);
* exit: the trend flips (the mirror of the repository's SMA-cross exit rule);
* the short side is the exact mirror; the planner ignores an `EXIT` when the book
  is flat.

Two fixed parameter pairs are also declared (`5/13` and `13/34`) and the grid is
deliberately small: a wide search over the same sample is how a backtest stops
being evidence.

Baselines, on the same windows: `SmaCrossStrategy` at 8/21, and a never-trade
floor. A strategy that cannot beat a zero-trade book after costs has no edge worth
running.

Every candidate carries its assumptions and limitations in its audit record, and
the runner shows them in the UI next to the state.

---

## 4. Running it

The runner lives behind a flag; without it the workstation is exactly as before.

```bash
# Offline: replay the platform's own stored bars (labelled HISTORICAL_REPLAY).
python -m axiom_forex.workstation.launch --simulate --paper-runner --paper-runner-source replay

# Normal workstation, with the runner panel present.
python -m axiom_forex.workstation.launch --paper-runner --paper-runner-source terminal
```

Useful options:

| Option | Meaning |
| --- | --- |
| `--paper-runner-database PATH` | the runner's durable paper database (default `<settings dir>/paper_runner.local.sqlite3`) |
| `--paper-runner-source terminal\|replay` | aggregate the terminal's bid/ask ticks, or replay stored bars |
| `--paper-runner-interval 60` | bar interval, in seconds, for the terminal feed |
| `--paper-runner-quote-age 5` | the newest tick must be younger than this, in seconds |
| `--paper-runner-quantity 0.10` | paper size per entry |
| `--paper-runner-candidate trend_pullback_v1_8_21` | which candidate id to run |

### Start

1. Open the workstation.
2. **Trading mode** → make sure it says `PAPER`. The runner refuses otherwise and
   says so; it never switches the mode for you.
3. **Overview** or **Positions** → the *Paper strategy runner* panel shows
   `STOPPED` or `BLOCKED` with the exact reason and the fix.
4. Press **Start runner**. If a guard fails, you get the reason and nothing is
   traded. If it starts, the panel changes to `RUNNING (PAPER)` and begins
   recording bars, decisions, and refusals.

### Stop

Press **Stop runner**, or close the workstation (Ctrl+C). Stopping places nothing
and cancels nothing; open paper positions stay in the local ledger, exactly as a
real book would.

### Restart

Start the workstation with the same `--paper-runner-database`. The runner
re-reads the durable state, restores the ledger and counters, and then **waits**
for an explicit Start. It never resumes trading on its own, and a restart never
duplicates an order: request ids are stable per run and decision index.

---

## 5. What the panel tells you

| Row | Meaning |
| --- | --- |
| State | `RUNNING (PAPER)`, `STOPPED`, `BLOCKED`, or `UNAVAILABLE` |
| Last tick / last signal / last fill | real instants, in UTC |
| Strategy + candidate status | which candidate, its parameters, and whether it is selected |
| Data | feed kind and data class — `TERMINAL_TICKS` (real broker ticks) or `HISTORICAL_REPLAY` |
| Cycles / bars / signals / orders / fills / refusals | counters, so a silent runner is visible |
| Why | the current reason code and a one-line explanation |
| Last decisions | the most recent reasons, oldest dropped first |

Every reason code is also persisted, so an empty screen is never the answer to
"why didn't it trade?":

| Reason | Meaning |
| --- | --- |
| `no_new_bar` | ticks are arriving, the current bar has not closed yet |
| `waiting_for_history` | not enough completed bars for this strategy yet |
| `hold_no_signal` | the strategy evaluated the bar and found nothing |
| `signal_without_action` | the signal matched the position already held |
| `stale_quote` | the newest price is older than the policy allows |
| `data_gap` | windows were skipped; the runner refuses to trade across a hole |
| `market_closed` | the declared calendar says the market is shut |
| `gate_refused` | a pre-trade risk or safety check refused the order |
| `no_execution_price` | a signal on the last bar, with no later price to fill at |
| `feed_disconnected` / `feed_exhausted` | the feed is gone, or the replay ended |
| `live_mode_active` / `candidate_not_selected` / `invalid_instrument_spec` | a guard blocked the start |
| `unresolved_order_present` / `intent_without_order` | the book or the journal is in doubt |
| `cycle_failed` / `state_recovery_failed` | a failure was caught and the loop stopped |

---

## 6. Fail-closed behaviour

| Situation | What happens |
| --- | --- |
| Mode is `LIVE`, no live path in this process | start refused (`live_path_not_configured`), nothing runs and no paper fill is simulated |
| Mode is `LIVE`, live path present but a check fails | start refused (`live_not_ready`) with the leg's own reason (not armed, account mismatch, unresolved order, …) |
| Candidate not selected | start refused (`candidate_not_selected`) |
| Instrument spec invalid | start refused; no size, cost, or tick size can be trusted |
| An unresolved paper order exists | start refused (`unresolved_order_present`) |
| An intent has no matching order | start refused (`intent_without_order`) until an operator looks |
| Newest tick too old | `stale_quote`, no order |
| A tick jumps a whole window | `data_gap`, no order; nothing is interpolated |
| The terminal stops answering | `feed_disconnected`, loop stopped |
| The calendar says closed | `market_closed`, no order |
| The gate refuses | `gate_refused`, recorded with the gate's own reason |
| The process dies mid-cycle | the durable order row and reservation settle or the next start blocks |
| No terminal session is open when Start is pressed | start refused (`terminal_not_connected`) with the hint to press Connect; nothing is built and nothing runs |
| The session has no read-only market data (simulator preview) | start refused (`market_data_unavailable`); a preview has no ticks to price |
| The provider setup document is missing or incomplete | start refused with the loader's own reason code (`provider_setup_unreadable`, `missing_swap_terms`, …) |
| The watched symbol is not in the terminal's catalogue | start refused (`symbol_unresolved`) — never substituted |
| Any other build failure | start refused (`runner_build_failed`); the page shows a plain sentence and the console shows the exception type, never the traceback |

The runner writes its **intent** before the paper engine executes anything. If a
process stops between the two, the next start finds an intent with no order and
refuses to trade — it does not assume the order failed.

---

## 6a. Trading a *real* broker symbol (the provider lane)

The fixture catalogue (`AAA/BBB`) exists so the machinery can be tested. To paper
trade the symbol your own terminal actually quotes, the runner is pointed at two
operator-supplied documents instead of the fixture:

```bash
python -m axiom_forex.workstation.launch \
  --paper-runner \
  --paper-runner-provider-setup ./provider_setup.local.json \
  --paper-runner-pair EUR/USD \
  --paper-runner-database ./paper_runner.local.sqlite3
```

Order of operations, and why:

1. **Connect in the browser first.** The runner builds its lane only when you
   press **Start**, because the instrument catalogue must come from the terminal
   you signed in to. Before that the panel says `waiting_for_terminal`, not
   "ready".
2. **The catalogue resolves the symbol exactly.** `axiom_forex/brokers/mt5/catalogue.py`
   looks for the setup's `broker_symbol` (for example `EURUSD.pro`) exactly, then
   `canonical + suffix`, then the bare six letters. Two matches is an ambiguity
   and a refusal, never a pick. Nothing is substituted.
3. **The broker's own numbers build the spec.** Digits, point, contract size,
   volume min/max/step, filling mode, and trade mode come from `symbol_info`; pip
   size, correlation group, margin multiplier, and spec version come from your
   setup document. A cross-check compares the two, and a difference in a trading
   term refuses the instrument (`catalogue_cross_check_failed`).
4. **Your setup supplies the rest.** Account currency and leverage, cost model,
   swap cards, risk limits, the entry-exposure cap, and the session calendar all
   come from `provider_setup.json`. Missing terms are named refusals:

   | Refusal | Meaning |
   | --- | --- |
   | `no_calendar_windows_for_pair` | the calendar says nothing about when this pair trades |
   | `missing_swap_terms_for_pair` | overnight financing would be unmodelled |
   | `missing_entry_exposure_limit` | no declared cap, so the reservation book refuses entries |
   | `missing_conversion_rate` | a leg the pair or its exposure needs cannot be derived |
   | `provider_setup_invalid` | a spec, cost, or symbol term did not validate |

5. **Conversion rates come from the broker's quotes, not from a constant.** A
   EUR/USD position in a USD account needs EUR→USD; that is the broker's own
   EUR/USD quote (labelled `DIRECT`), its inverse (labelled `INVERSE`), or two
   quotes through a shared currency (labelled `CROSS`). Each lookup re-reads the
   tick and applies the same freshness policy as the lane, so a frozen feed
   produces `STALE_RATE` rather than a stale number. A leg that cannot be derived
   is `MISSING_RATE`, and the stack refuses to build.

6. **The feed is pinned to the resolved symbol.** The runner never re-resolves on
   a later poll: a rename, a second suffix variant, or a broker symbol change
   makes the feed fail closed instead of quietly trading a different instrument.

Fills are still produced by the **local paper engine** and written to the local
ledger. The lane holds no adapter and no order surface.

---

## 6b. Live execution (off by default, and a real path when on)

Live is not a label. `axiom_forex/paper_runner/live.py::LiveLeg` sequences the
components that already existed, and refuses to skip any of them:

```
approved strategy signal
  → planner (same spec, same calendar, same quote)
  → PreTradeGate (the identical request the paper path would send)
  → as_live_decision (an envelope: it cannot upgrade a refusal)
  → ExecutionRouter.route_live (mode LIVE + confirmed + armed + adapter present)
  → MT5TradingAdapter.submit (account identity, trading permission, symbol terms,
      netting conflict, quote drift, volume rules, free margin, strategy approval)
  → order_send
  → durable record + audit
  → reconciliation against MT5 positions
```

Before anything is built, the leg reports **every** blocker it can see:
`mode_not_live`, `live_not_confirmed`, `live_router_missing`,
`live_adapter_missing`, `mt5_not_connected`, `mt5_trading_disabled`,
`live_not_armed`, `live_account_mismatch`, `unresolved_order_present`,
`strategy_not_approved_for_live`, `live_symbol_spec_mismatch`,
`duplicate_request`, `live_positions_mismatch`, and `live_fill_not_booked` after a
fill could not be booked.

Behaviour that is not negotiable:

* **Switching modes never submits an order.** The leg has no hook on a mode
  change; a test flips the mode object and asserts that nothing was sent.
* **One order per plan.** The request id is recorded before submission, and a
  second attempt with the same id is a `duplicate_request` refusal.
* **The broker's fill is booked into AXIOM's ledger.** Quantity and price come
  from the terminal's answer (a partial fill is booked as the size that traded),
  so the pre-trade gate sees the same exposure live as it did on paper. A fill
  that cannot be booked blocks the next order instead of being dropped.
* **The ledger and the terminal must agree.** Every cycle compares positions
  before it will send; a difference is a `live_positions_mismatch` blocker, never
  a silent correction.
* **An unknown outcome is unresolved, and is never resent.** The adapter disarms
  itself, the leg reports `unresolved_order_outcome`, and the next attempt is
  blocked until broker history explains the first one.
* **The gate is not bypassed.** `PreTradeGate` is paper-only by construction — it
  rejects a request whose target mode is `LIVE`. The live leg therefore asks it
  the *same* question the paper path asks, and only wraps the approved intent. The
  adapter then re-validates that the intent it is asked to send is byte-for-byte
  the one the gate approved (`mt5_intent_differs_from_approved_intent`).

### The operator entry point

Live execution runs in the operator's own process, from the command line, because
arming is a decision and the browser session is observation-only:

```bash
python -m axiom_forex.cli live-run \
  --mt5-settings-path C:/axiom/mt5.local.json \
  --database-path C:/axiom/axiom.local.sqlite3 \
  --provider-setup C:/axiom/provider_setup.json \
  --paper-runner-pair EUR/USD \
  --paper-runner-candidate trend_pullback_v1_8_21 \
  --confirm "ENABLE LIVE TRADING <your login>" \
  --paper-runner-max-cycles 1
```

`paper_runner/live_run.py::run_live_session` refuses to assemble anything unless
the adapter is **already connected and already armed**, so the runner can never
arm a broker by being started. It builds the same platform, planner, gate, and
strategy the paper lane uses, adds the live leg, runs a bounded number of cycles,
then reconciles positions and reports what was sent and booked.

### Operator steps to enable live execution

1. Sign in to MetaTrader 5 yourself, with the account selected. AXIOM never logs
   in and never asks for a password.
2. Start the workstation (not `--simulate`), press **Connect**, and confirm the
   account shown is the one you intend to use.
3. Import your tick history, run `paper-evaluate`, and get a candidate to
   `SELECTED_FOR_PAPER`. Live uses the same approved strategy as paper.
4. Run the paper lane long enough for you to trust the plumbing.
5. In the terminal, enable algorithmic trading, and in the settings document
   enable trading. Then, in **Trading mode**, switch to `LIVE` and type the
   confirmation. The switch places nothing.
6. Arm the adapter (`ENABLE LIVE TRADING <login>`). Live orders require the mode,
   the confirmation, **and** arming: any one missing is a refusal. `live-run`
   takes the phrase on the command line and arms nothing else.
7. Watch the reconstruction: every submission, outcome, and reconciliation is in
   the audit trail and in the live panel.

If any of that is not true, the leg reports the blocker and sends nothing.

---

## 6c. What the runner is allowed to read from the session

The runner never touches an order surface, and it does not reach into the
workstation's session for one. `ObservationSession` exposes exactly two
read-only accessors:

| Accessor | Returns | Used for |
| --- | --- | --- |
| `session.market_data_source()` | the connected source's `MT5Terminal`, or `None` | the instrument catalogue (`symbol_names`, `symbol_info`) and the tick feed |
| `session.market_data(freshness)` | the quote/ticks surface, or a `ConnectionRefused` | pricing a paper fill from real broker ticks |

Ask for anything else — `session.terminal`, which does not exist — and the runner
now fails as a *blocker* (`terminal_not_connected` / `runner_build_failed`) rather
than a worker-thread `AttributeError`. The returned terminal has `quote`, `tick`,
`symbol_names`, and `symbol_info`; it has no `order_send`, no arming, and no
account switch, so a runner priced from it cannot send anything even by mistake.

While the runner is not running, the panel lists every remaining blocker with a
stable code, a plain sentence, and where to fix it: `terminal_not_connected`,
`provider_setup_missing`, `no_imported_history`, `strategy_not_evaluated`, and
whatever the runner or the live leg itself reports. **Start runner** stays
available, so connecting and pressing it again is the whole recovery path.

---

## 6d. The provider setup document

`--paper-runner-provider-setup` points at one local JSON document of broker terms,
schema `axiom-provider-setup-v1` (`axiom_forex/config/provider_setup.py`). The
workstation builds it on **Provider setup** (`/provider-setup`):

| Field group | Source | Notes |
| --- | --- | --- |
| `account.currency`, `account.leverage` | **terminal** | read from `account_info()`; the terminal wins if the form disagrees |
| `symbols[].broker_symbol`, `suffix`, `pip_size` | **terminal** | read from `symbol_info()`; an unquoted symbol is refused by name |
| `account.account_id`, `initial_capital`, `entry_exposure_limit` | operator | a label, your starting balance, your hard entry cap |
| `costs.*` | operator | commission, slippage, financing; never defaulted to zero |
| `rollover.*` and `swap[]` | operator | daily cutoff, triple-swap weekday, swap points |
| `calendar.*` | operator | explicit session windows with `valid_from`/`valid_until`, plus holidays |
| `conversion_rates[]` | operator | required when the account currency is not the pair's quote currency |
| `risk.*` | operator | every limit the pre-trade gate enforces |
| `execution.*` | operator | lot size, position count, whether shorts are allowed |

The page validates with `load_provider_setup` — the runner's own loader — so a
saved file is one the runner accepts, and a refusal names the exact field
(`costs.commission_rate`, `calendar.valid_from`, …). Nothing is written until the
whole document validates, and an existing file is only replaced when the operator
ticks the replace box.

The file lives in the project root as `provider_setup.local.json`, is ignored by
Git through `*.local.json`, and the Windows updater neither reads nor moves it.
`profiles/provider_setup.example.json` documents the schema with placeholders and
is explicitly non-runnable; it is a different document from
`profiles/provider_onboarding_template.json`.

---

## 7. Current state, honestly

* Everything above is implemented and covered by tests that run offline against
  synthetic fixtures. No test contacts a terminal, an account, or a credential.
* **No market data is included.** `imports/` holds a 6-bar format example whose
  values are illustrative. With nothing imported, the audit says so, no candidate
  can be selected, and the runner stays `BLOCKED`. That is the intended default.
* **No profitability result is claimed.** No candidate has been run on real
  licensed history here, so there is nothing to report beyond the rules and the
  fixtures. Win rate, net expectancy after costs, drawdown, trade count, and
  losing periods are all *reported* whenever an evaluation runs — including when
  they are bad.
* The locked test window for any dataset is spent once. Do not re-run it looking
  for a better answer; that would turn the test set into a tuning set.
* The workstation's default paper platform uses the repository's synthetic
  instrument catalogue (`AAA/BBB`). The provider lane
  (`--paper-runner-provider-setup`) is the path for a real symbol, and it needs
  *your* `provider_setup.json` plus a connected terminal; without them nothing is
  substituted and the lane refuses to build.
* The live leg and `live-run` are proven against the **fake** MetaTrader 5 module
  only: no real order has been placed, no account has been contacted, and no real
  `order_send`/`order_check` result has been observed. Broker-specific terms
  (exact symbols and suffixes, filling modes, retcodes, margin behaviour) remain
  unverified until you run it yourself on Windows.
* Untested here (needs Windows and a terminal you signed in to): the real tick
  stream, broker symbol suffixes, filling modes, and behaviour across a live
  session break.

---

## 8. Where the code lives

| Module | Responsibility |
| --- | --- |
| `paper_runner/feed.py` | terminal tick aggregation and offline replay, with explicit stale/gap/disconnect events |
| `paper_runner/strategy.py` | the candidate grid, the baselines, and candidate status derivation |
| `paper_runner/runner.py` | the loop, its guards, the order path, restart, and the read model |
| `paper_runner/journal.py` | the write-ahead intent and the run state in SQLite |
| `paper_runner/host.py` | the single owner thread, so the UI and the loop never race |
| `paper_runner/evaluation.py` | chronological OOS, walk-forward, baselines, criteria, locked-test claim |
| `paper_runner/data_audit.py` | what history exists, its provenance, coverage, and problems |
| `paper_runner/provider.py` | the paper platform built from a catalogue-resolved symbol and your setup terms |
| `paper_runner/conversion.py` | account-currency rates derived from the broker's own quotes, with provenance |
| `paper_runner/live.py` | the live leg: blockers, one gated submission, ledger booking, reconciliation |
| `paper_runner/live_run.py` | the operator's live entry point: same strategy and gate, bounded run, reconciled report |
| `brokers/mt5/catalogue.py` | exact symbol resolution and spec/cross-check validation |
| `config/provider_setup.py` | the strict operator-terms document loader |
| `ingest/mt5_ticks.py` | MT5 tick export → canonical bid/ask bars, with provenance |
| `execution/planner.py::plan_at_quote` | the live-quote planning path (shared with the backtest planner) |

Tests: `tests/test_forex_paper_runner.py`, `tests/test_forex_paper_evaluation.py`,
`tests/test_paper_provider_lane.py` (provider lane + live leg against the fake
terminal), `tests/test_forex_live_runner.py` (runner → live leg → `order_send` →
ledger booking), `tests/test_mt5_tick_import.py`, and the runner panel cases in
`tests/test_workstation_ui.py`.
