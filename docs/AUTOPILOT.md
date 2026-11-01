# The autopilot: evaluate, select, then follow

One page, one start, one stop. The autopilot runs three steps in a fixed order and
cannot do anything else:

1. **Evaluate** the declared catalog on bars just read from the terminal, using the
   catalog's own 60/20/20 split and frozen-parameter walk-forward folds.
2. **Select** among those rows with predeclared rules, on train and validation
   evidence only, writing a reason for every candidate that is not chosen.
3. **Follow** the selected rows as a paper test: real broker quotes, fills simulated
   locally, a ledger and a state file on disk.

There is no fourth step inside the autopilot. It holds no adapter, no order object
and no credentials, cannot change the execution mode, and cannot arm live trading.

Once a selection is running, a separate bridge (`live_autopilot.py`) can offer the
**same** strategy to the live workflow that the Live execution page already owns.
That bridge adds a start and a stop button and nothing else: it cannot arm, cannot
size, cannot widen a limit, cannot pick a symbol and cannot send an order. See
"Running the selection on LIVE" below.

---

## Why the selection is out-of-sample within sample

The evaluator's walk-forward folds cover the **whole** series, so a fold can reach
into the locked test window. The selector therefore counts only folds whose `end`
is at or before the validation boundary, and the boundary is derived from the
evaluated series' own timestamps (`validation_boundaries`). Two consequences:

* a row whose boundary cannot be established is **rejected**, not assumed
  (`validation_boundary_unknown`);
* `select()` never reads `windows["test"]` at all. A test window result cannot move
  a row up or down the plan.

Once a plan exists, re-selection is a **stop/start cycle**. The paper loop never
re-selects mid-run: swapping a strategy under an open position would make every
number in the ledger meaningless.

## The rules, and their fingerprint

| Rule | Default | Meaning |
| --- | --- | --- |
| `min_folds_positive` | 1 | positive walk-forward folds *inside* train+validation |
| `max_drawdown` | 0.25 | validation drawdown a row may show |
| `max_concentration` | 0.6 | share of the validation result one trade may carry |
| `max_per_symbol` | 1 | one row per symbol |
| `max_per_timeframe` | 2 | two rows per timeframe |
| `max_per_structure` | 1 | one row per rule structure |
| `max_selected` | 6 | rows in one plan |
| `require_promoting_origin` | on | only terminal history may promote a strategy |

The policy is serialised into every plan with a SHA-256 fingerprint over those
numbers, so a plan can be reproduced and compared later. Ranking is deterministic:
`(-score, candidate_id, symbol, timeframe)`. Caps are applied in ranked order and
each row that a cap held back says **which** cap it hit. An empty selection is a
result: the rules are not relaxed to fill the cap.

Also fixed in the record: the evaluation budget, the cost model per symbol (spread
from the bars, slippage and commission labelled as estimates or assumptions, never
zeroed), and the data origin. A plan built on anything but the terminal's own
history selects nothing.

## What the page shows

* the state (`IDLE` / `PREPARING` / `RUNNING` / `STOPPED` / `BLOCKED`) and, when it
  is blocked, the step that stopped it;
* the selection: counts, policy fingerprint, and a table of every candidate with
  its decision, rank, reason code and the wording for it;
* the active strategy per symbol and timeframe, with the trades and positive folds
  it was chosen on;
* feed health: the last quote per symbol, its age, its bid/ask and whether it is
  fresh enough to use;
* open simulated positions, entries, simulated closes and the local ledger path;
* the last decision per symbol and timeframe, including every refusal;
* **observed** latency: the selection pass, the evaluation, and the paper loop's own
  decision time, with the sample count. These are measurements from this process on
  this machine; they are not a service level, and order submission cannot be timed
  here because this loop cannot submit an order.

## Handling the things that go wrong

| Situation | What happens |
| --- | --- |
| No terminal | Refused before any work: `no_terminal`, with the instruction to connect |
| No usable bars | `no_usable_history`; nothing is replaced with sample data |
| Stale / missing quote | That cycle is skipped, counted, and shown as `stale_quote` |
| Half-formed bar | Dropped: a decision only ever sees completed bars |
| Market closed | Reads as missing quotes, so the loop idles; nothing is invented |
| Restart | Open positions come back from the state file, marked `restored_after_restart` |
| Stop | The loop ends; **open positions stay open and stay visible** |
| Flatten | A separate, explicit click; closes the *simulated* book only |
| Evaluation fails | `evaluation_failed`; no row is selected and nothing is traded |

## Kronos is optional, and can only veto

Off by default. When the operator turns it to `VETO`, the loop may ask for a
forecast for a signal it has already produced, and the answer can **cancel** the
entry. It can never create an entry, change a direction, size a position, move a
stop, or reorder the plan: the selector does not import the gate, and the gate's
output is a refusal that only skips a trade.

* A forecast is read from what the operator already recorded on `/kronos`. The
  trading loop never loads a model and never runs inference, so a slow model can
  never stall a decision.
* No stored forecast, no model, no readable payload — the absence is **recorded**,
  the signal proceeds, and the page counts it. Only if the operator explicitly asks
  for Kronos to be *required* does an absent forecast skip the trade, with the
  reason `kronos_required_but_unavailable`.
* A forecast whose own distribution at the horizon includes no move confirms
  nothing and vetoes nothing (`kronos_no_direction`).
* Every forecast stays labelled `FORECAST_ONLY_NOT_A_SIGNAL`. A vetoed trade is not
  evidence that Kronos is right.

## Taking a selection to LIVE

The autopilot does not do it, and the page says so. The existing path is unchanged:

1. select and confirm LIVE on the Trading mode screen (sends nothing);
2. build the live plan and review it on the Live execution page;
3. arm it: the phrase `ENABLE LIVE TRADING`, the fingerprint of the account the
   terminal is on *now*, and the review acknowledged;
4. the strategy loop runs, and **every** order still passes the broker adapter's own
   per-order preflight — mode, arming, account identity, symbol specs, volume step,
   margin and the risk limits.

The handoff card lists what is missing for each of those, proposes the candidate and
pair of the current selection, and deliberately proposes **no** quantity or risk
limit: the broker's tradable step, the account's currency and the operator's own
limits are not known here, and guessing one would be worse than leaving it blank.

Passing the screen on history is not a live readiness verdict.

## Running the selection on LIVE

The bottom card on `/autopilot` is a bridge to the live workflow `/live` already
built. It is a *front door*, not a second order path:

* **It starts the same session `/live` starts.** `LiveTrading.start_session()` is
  called; that method refuses unless the adapter is armed and every blocker on the
  live page is clear. Nothing is re-implemented and nothing is checked twice with a
  weaker rule.
* **It cannot arm.** The plan, its quantity, its position cap, its daily-loss cap,
  the review acknowledgement, the account fingerprint and the phrase
  `ENABLE LIVE TRADING` all stay on `/live`. The bridge only reads the plan that was
  already written there.
* **It refuses unless the plan is the selected strategy, on its symbol, at its bar
  size.** A strategy is (rule, symbol, timeframe), so each of the three is compared
  and each disagreement is its own named blocker: `plan_not_for_selected_strategy`
  when the `candidate_id` differs, `plan_pair_not_selected` when the symbol differs,
  `plan_timeframe_not_selected` when the bar size differs. The card names both
  values. Nothing is re-timed, re-symbolised or averaged to make a start possible.
* **PAPER can never send.** A paper lane has no live leg at all, and the card's
  "sending" chip requires the live path's own verdict *and* a LIVE mode.
* **A signal still passes every per-order check**: the execution gate, the risk
  limits, the exact-account confirmation, the plan's watched stop, the broker-side
  stop this build attaches and verifies, and the unresolved-order lockout. A refusal is displayed with its reason code and is not
  retried; an order whose outcome is unknown stops the session instead of resending.
* **Stopping disarms and leaves positions open.** The message says so, and the open
  positions are listed on the card.

What the card shows, all of it read from the live path rather than inferred:

| Field | Where it comes from |
| --- | --- |
| Active strategy, pair, broker symbol, timeframe | the armed plan, plus the running selection |
| Plan protective stop | the armed plan, with the sentence that it is a limit AXIOM watches, not a broker stop |
| Broker-side stop loss | the MT5 adapter's verification of the stop the broker echoes back, and the tickets it reports as unprotected |
| Signal, and what became of it | the runner's `last_signal` and the live leg's history |
| Order result or refusal | `LiveOutcome.as_result()` — status, reason code, unresolved flag |
| Feed freshness | the running feed's newest accepted quote, with its age |
| Measured latency | decision/submission durations the runner recorded; otherwise "not measured" |
| Kronos | its policy and its vetoes — never a trade |

A duration the live path did not record is printed as *not measured*, never as zero;
the only timings this process adds are the session-start and page-read durations it
measured itself.

The blockers are shown one at a time and would each switch submission off on their
own: no selection, a selection that is not running, missing live wiring, no written
plan, a plan for another strategy, a plan for another symbol, a plan on another bar
size, the mode not being LIVE, the adapter not connected, the plan not armed, and
every blocker the live page itself reports -- including a missing protective stop, a
bar size this stack does not read, a position beyond the declared stop, a position
the broker reports without a stop loss, and a symbol with no usable per-symbol
distance in `protective_stops`.

Passing the screen on history is not a live readiness verdict, and neither is this
bridge: it decides *whether a session may start*, not whether a strategy is good.

## Files

* `axiom_forex/strategies/catalog/selection.py` — the policy, the fold rule and the
  selector.
* `axiom_forex/strategies/catalog/autopilot.py` — the three steps, the state
  machine, the latency measurement and the handoff description.
* `axiom_forex/strategies/catalog/kronos_gate.py` — the veto-only forecast input.
* `axiom_forex/strategies/catalog/live_autopilot.py` — the bridge to the existing
  live workflow: selection prerequisites, the live path's own blockers, and the
  read model behind the LIVE card.
* `data/catalog/catalog-evaluation.json` — the evaluation, as the catalog page
  writes it.
* `data/catalog/catalog-selection.json` — the selection: every decision, the policy
  fingerprint, the data origin and the Kronos mode, with `test_window_used: false`.

All of them are local and Git-ignored, and none contains a credential, an account
number or a balance.

## What none of this claims

* No candidate is profitable. A selection is a short-listed hypothesis that passed a
  declared screen and a declared set of rules on history.
* The results are historical estimates on the bars this deployment could supply,
  under the stated cost assumptions. A fixture origin says so on the page.
* The paper fills are simulated. They are not broker fills and not a track record.
* No latency figure here is a promise, and nothing on this page is measured on the
  operator's Windows/MT5 machine.
* Stopping the loop, changing the mode, or loading Kronos never places or closes an
  order.
