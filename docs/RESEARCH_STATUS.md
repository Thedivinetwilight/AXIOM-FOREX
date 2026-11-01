# Catalog research status

**Status of the declared catalog run: not executed. No candidate has been tested in
this environment, and no result in this file is a measurement of a strategy.**

This file records what was declared, what was actually run, and why the rest was
not. It exists so the gap is visible rather than filled with something that looks
like a result.

## What was declared

`axiom_forex.strategies.catalog.candidates.catalog()` declares **1,566** candidates
across six families:

| family | declared |
| --- | ---: |
| multi_timeframe | 522 |
| trend | 288 |
| mean_reversion | 252 |
| momentum | 216 |
| breakout | 144 |
| volatility | 144 |
| **total** | **1,566** |

Each candidate is one entry rule × one exit rule × zero or one filter, and every
declared id is accounted for by `axiom_forex/strategies/catalog/audit.py` whether it
was planned by a run or not.

## What was actually run

Nothing. The evaluation screen reads bars from the deployment's own MetaTrader 5
terminal, and this environment has no terminal, no account, and no bar files. The
one "result" produced here is therefore the empty one:

```
$ python -m pytest -q tests/test_catalog_audit.py       # the audit code: passes
$ python -c "..."                                        # build_audit(None)
declared 1566 · tested 0 · skipped 0 · failed 0 · not considered 1566
```

Every one of the 1,566 candidates is reported `NOT_CONSIDERED` with the reason
`no_evaluation_record`. That is an absence of a test, not a pass, and the audit
module's own wording says so. **Do not** fill this table with fixture bars: the
fixtures in `fixtures/` are synthetic instruments built to exercise code paths, and
reporting them as history would be a false claim about the past.

## History coverage

| item | value |
| --- | --- |
| symbols available | none in this environment |
| timeframes available | none in this environment |
| first bar / last bar | n/a |
| bars per series | n/a |
| locked test window | not read, because nothing was evaluated |

The split rules are unchanged from `evaluate.py`: chronological 60 / 20 / 20, the
screen reads train and validation, and the **locked test window is read once and
never fitted**. No candidate has been tuned against it, here or by this change.

## Incomplete cost inputs

The evaluator labels every series with the cost inputs it could and could not
establish. Where they are missing, a row carries `costs_incomplete` and no result
from it may be called cost-complete:

| input | source | state in this environment |
| --- | --- | --- |
| spread | symbol's reported spread at evaluation time | **not available** |
| slippage | fixed estimate, `slippage_pips` | estimate, not measured |
| commission | broker schedule | assumed zero unless supplied |
| swap / rollover | symbol's swap terms | unknown |
| contract size, point | symbol specification | **not available** |

So even on a machine that *does* have history, a run without commission and swap
terms produces rows marked incomplete. Those rows are evidence about the past under
stated assumptions, and nothing stronger.

## Kronos

Kronos was **not** installed, not run, and influenced **no** selection. It remains
an optional, user-triggered component: it cannot create an order, it is never
required, and it may only affect selection if it shows out-of-sample value against
the simple baselines — which has not happened, because no out-of-sample measurement
exists. Its absence changes nothing above.

## Verified in this environment vs. what needs the operator's machine

Verified with fakes and unit tests:

* the audit folds a record into one row per declared candidate, including
  not-planned ones, and refuses to call an empty record a result
  (`tests/test_catalog_audit.py`);
* the evaluator's split, budget, skip reasons, and `costs_incomplete` labelling
  (`tests/test_catalog_evaluation.py`);
* the page renders the audit summary, including when no audit exists.

Not verified anywhere yet — needs a Windows machine with MetaTrader 5 and history:

* any actual measurement of the 1,566 declared candidates;
* real coverage (symbols, timeframes, first/last bar);
* real cost inputs, so that a run can be called cost-complete.

## How to run it there

Both steps are buttons in the workstation, not command lines, because the
evaluation reads the terminal through the same adapter the rest of LIVE uses:

1. **Catalog page → “Evaluate the catalog on this history”.** Runs the declared
   catalog over the history the terminal can supply, inside the existing time
   budget, on the train and validation windows. It writes `catalog-evaluation.json` beside the workstation's
   data and is safe to re-run; a re-run replaces the record.
2. **Catalog page → Audit the whole catalog.** Folds that record into
   `catalog-audit.json`, one row per declared candidate, with `TESTED` / `SKIPPED` /
   `FAILED` / `NOT_CONSIDERED` and the reasons. It reads the record and changes no
   result, so it cannot promote anything.
3. **Read the summary.** It is shown on the page and in the downloaded record, and
   the same numbers appear in the table above once a real run exists.

A run that finds no usable history, or that runs out of budget, is reported as
skipped with that reason — never as a pass. If the page has no audit at all, it
says so instead of showing zeros that look like a clean sweep.

The two records are ordinary local files, they are replaced on each run, and
neither can promote a candidate to live trading.

> A historical evaluation on the bars this deployment could supply. It is evidence
> about the past under the stated cost assumptions, not a forecast, and it does not
> show that any candidate will do well on a live account. No candidate here is
> claimed to be profitable.
