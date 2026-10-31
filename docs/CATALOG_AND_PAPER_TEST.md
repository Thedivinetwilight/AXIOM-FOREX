# The strategy catalog and the live-market paper test

Two things live here, and they are deliberately separate:

1. a **catalog** of 1,566 declared, deduplicated FX candidates, screened on the
   terminal's own historical bars;
2. a **paper forward test** that follows the screened rows on live broker quotes,
   with fills simulated locally and a ledger on disk.

Neither can send an order. The paper test is given a read-only terminal — symbols,
specs, ticks and rate history — and nothing else.

---

## What the catalog is

| Count | Meaning |
| --- | --- |
| 1,566 | candidates: one declared parameter set each, unique signature |
| 480 | distinct rule *structures* (entry + exit + filter, parameters removed) |
| 16 / 6 / 5 | entry / exit / filter blocks, each with a fixed declared grid |
| 6 | families: trend, momentum, mean reversion, breakout, volatility, multi-timeframe |

The three numbers are reported separately and never added together. A structure
with one parameter set counts as one candidate: there is no renamed padding, and a
candidate's id is a hash of its signature, so adding a candidate cannot renumber
the others and renaming a label cannot look like a new idea.

Every candidate carries: a stable id (`cat-…`), a plain-language summary, its
entry, exit and filter logic in words, its risk assumptions (one position at a
time, position-level exit, operator's fixed lot, ATR used for exits and never for
sizing), the timeframes it is offered on, and the bars it needs before it may
decide anything.

### Multi-timeframe

A multi-timeframe candidate reads a *coarse* bar built by merging the finer bars,
and only ever sees **completed** coarse bars — never the bucket it is inside. The
ratio is declared (4× or 12×); a 12× coarse bar on a 4h series would be a two-day
bar, so those combinations are not offered.

### Simulation conventions

* A decision is taken on bar `i` using bars `≤ i` only.
* The fill happens at the **open of bar `i+1`**: ask for a buy, bid for a sell,
  plus the stated slippage against the trade.
* Stops, targets and trailing levels are checked inside each later bar with that
  bar's high and low. If a bar touches both the stop and the target, the **stop**
  fills — the pessimistic reading, and the only defensible one without ticks.
* Costs are the modelled half-spread plus slippage on each side, plus commission
  and swap when they can be priced. Anything that cannot be priced is **labelled**
  in the report; it is never silently zero.
* A position still open at the end of a window is closed at that window's last bar
  and flagged `FORCED`. It is counted, not hidden.

A thousand candidates were tried. The report says so next to the survivors,
because at a 5% false-positive rate roughly one in twenty rows looks positive on
noise alone.

## What the evaluation does

1. **Splits 60 / 20 / 20** chronologically into train, validation and test, and
   simulates each window separately so a position cannot leak across a boundary.
2. **Screens on train and validation only**: at least `min_trades` in both, net
   positive after costs in both, validation drawdown under the limit, and no single
   validation trade carrying most of the result.
3. **Measures the test window once**, for the report. It never feeds the screen,
   the ordering or the paper set. Parameters are declared before any data is read,
   so there is nothing to tune on the final window.
4. **Walk-forwards the survivors** with parameters frozen (four folds, up to forty
   rows), as a stability reading rather than a second chance.
5. **Reports what did not run**: `SKIPPED` with a reason (no history, too few bars,
   out of budget, cancelled) or `FAILED`, counted separately from `TESTED`.

A run on one symbol and one timeframe takes roughly half a minute for the whole
catalog on a normal laptop; the worker thread is bounded by a wall-clock budget.

## Monday: exactly what to click

1. Start the workstation and press **Connect** on the Setup page. Sign in inside
   MetaTrader 5 if the terminal asks — AXIOM never asks for a password.
2. Open **Quick setup**. The *Catalog and the Monday paper test* card lists the
   forex symbols your terminal reports, majors first. Metals and share CFDs are
   listed as excluded, with the reason.
3. In **1 · Evaluate the catalog**, pick one to three symbols (Ctrl/Cmd-click), pick
   `15m` or `1h`, and press **Evaluate the catalog on this history**. It runs in the
   background on the terminal's own rate history; reload the page for the result.
4. Read the result line: tested / skipped / failed, how many passed the screen, and
   the data origin. If nothing passed, that is the answer — the page says so, and
   the paper test stays disabled because there is nothing screened to follow.
5. Press **Start live-market paper test**. It builds from the screened rows in the
   stored evaluation, refuses a symbol the terminal no longer offers, and starts
   only if quotes are fresh.
6. Watch: quote freshness per symbol, the active symbols, the latest decision per
   row, simulated orders and fills, and open paper positions. **Stop the paper
   test** ends it; **Simulate closing open positions** flattens the paper book.

### What the page refuses, on purpose

| Situation | What happens |
| --- | --- |
| No evaluation on record | start is refused (`no_evaluation_on_record`) |
| Nothing passed the screen | start is refused (`no_screened_candidate_in_the_record`) |
| A second Start while running | refused (`paper_test_already_running`); the live run is untouched |
| Quote older than the allowance (120 s default) | no fill; the skip is recorded (`stale_quote`) |
| Symbol no longer offered, or no specs | dropped from the watch list with a reason |
| History too short for a candidate | `waiting_for_history` — the test collects, and never calls it a backtest |
| Record older than 14 days | refused as stale; rerun the evaluation |

The paper test also refuses to run without a terminal at all: its only price
source is the broker's own quote, and a missing quote is never replaced.

### The 24-hour qualification clock

The run feeds a qualification record one heartbeat per cycle, and that record is
what decides whether LIVE becomes available afterwards. It counts a heartbeat only
when the cycle was genuinely healthy: the terminal answered, the quote was fresh,
and a decision was actually evaluated. Anything else pauses the clock and books a
reason — `stale_quote`, `terminal_disconnected`, `runner_stopped`, `runner_restarted`,
`runner_unresponsive` (a gap longer than five minutes), `market_closed`,
`cycle_error`. A cycle that raised is also recorded as a failure, which fails the
test outright under the default criteria.

The default criteria (fixed when the test starts, stored with the record):

| Criterion | Default |
| --- | --- |
| Counted healthy time | at least 24 h |
| Wall-clock span | at least 24 h (a jumped clock cannot compress the day) |
| Share of the span paused | at most 30% |
| Share of heartbeats with a stale quote | at most 25% |
| Decisions evaluated | at least 1 |
| Recorded failures | 0 |
| Runner restarts | at most 5 |

Stopping the runner pauses the record; starting again reopens it and the stopped
period is booked as `runner_stopped`, so counted hours survive a pause. A record
that failed a criterion is never reopened: a new test starts and says which verdict
it replaced. A passing report makes LIVE *available* — nothing more. There is no
code path from this runner to an order, and passing does not change the mode or arm
anything.

### Where it writes

* `data/catalog/catalog-evaluation.json` — the evaluation record: counts, cost
  labels, data origin, per-row results, and the paper set.
* `data/catalog/paper/catalog-paper-ledger.jsonl` — one line per event and fill.
* `data/catalog/paper/catalog-paper-state.json` — status, watches, open positions.
* `data/catalog/qualification.json` — the 24-hour record: counted and paused
  seconds by reason, heartbeat and stale counts, decisions, failures, restarts,
  the fixed policy, and the verdict.

All three are local, Git-ignored, and contain no credential, no account number and
no balance.

## What none of this claims

* No candidate is profitable, and a passing screen is not an edge.
* Results are historical estimates on the bars this deployment could supply, under
  the stated cost assumptions. If the data origin says `SAMPLE_OR_FIXTURE_DATA`,
  the numbers describe a sample, not a market.
* The paper test's fills are simulated from real quotes. They are not broker fills,
  not a track record, and not evidence of live readiness.
* Live execution is a separate mode with its own gates. The paper test does not
  arm it, does not change the mode, does not touch the account, and cannot reach an
  order surface at all. Passing its 24-hour criteria only makes the live page
  available; one day of paper running is an operational check of the pipeline, not
  proof of profitability or safety.
