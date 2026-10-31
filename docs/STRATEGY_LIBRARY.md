# The strategy library

`/strategies` is a research index: documented strategy *templates*, the parameter
variants AXIOM is willing to attempt, a durable ledger of every attempt, and the
multi-testing control that decides whether any of it means anything. It cannot
choose a strategy for you, size a position, place an order, or change an execution
mode.

## 1. What a card is

Each entry is a `StrategyCard` (`axiom_forex/strategies/library/cards.py`) holding:

| Field | Why it is there |
| --- | --- |
| `rules` | The template written out step by step, so it can be audited rather than trusted |
| `parameters` + `grid` | The parameters and the **predeclared** values a variant may try |
| `timeframes` | The only timeframes the template is analysed on |
| `horizon_bars` | The holding horizon the rules were described for |
| `assumptions` | Fills, costs, gaps, calendar, and the locked-test rule |
| `limitations` | Where the shape is known to fail |
| `source` | A real citation (paper or textbook), or `DOCUMENTED_FAMILY` with no performance claim |
| `licence` | `TEMPLATE_DERIVED` — original code written from a published description |
| `requires` | The data the template needs: `BID_ASK_BARS`, `SWAP_RATES`, `MULTI_PAIR`, … |

**No proprietary code.** Nothing is copied from a vendor expert adviser, a
decompiled `.ex5`, or a paid strategy. `PROPRIETARY_NOT_USED` exists as a label
precisely so a card can say "this is not here".

**No popularity-as-proof.** Every card starts `TEMPLATE · untested`. A citation is a
reason to test a shape; it is never evidence that the shape works on this broker's
spreads.

### The catalogue today

| Template | Family | Timeframes declared | Evaluable |
| --- | --- | --- | --- |
| Moving-average crossover (Murphy 1999) | trend | 1m–1d | yes |
| Time-series momentum (Moskowitz, Ooi & Pedersen 2012) | trend | 15m–1d | yes |
| Donchian channel breakout | breakout | 5m–1d | yes |
| Bollinger band reversion (Bollinger 2001) | mean-reversion | 1m–1h | yes |
| ATR-scaled channel breakout (Wilder 1978) | volatility | 5m–1d | yes |
| Session range breakout | session | 5m–1h | yes |
| Swap-adjusted carry with a trend filter (Lustig, Roussanov & Verdelhan 2011) | carry | 1h–1d | **no — no historical swap rates are imported** |
| Relative-value pair spread reversion | mean-reversion | 5m–1h | **no — single-pair evaluation only** |

The two unavailable cards stay in the catalogue with the missing input named. That
is the point: a gap should be visible, not filled with an approximation.

## 2. Variants, and the honest count

A "large library" here is **variants of a small number of documented templates**.
Each card declares a grid; `variants_for()` builds the valid combinations, capped at
`MAX_VARIANTS_PER_CARD = 12`. Combinations that contradict the template (a fast
period that is not below the slow one) are dropped.

The page prints four numbers and never mixes them up:

* templates (8), families (6), evaluable templates (6);
* **variants** (42) — predeclared parameter combinations;
* **attempts recorded** — every evaluation ever run, including the failures.

There is no "2,000-strategy library" anywhere, because trying 2,000 combinations and
reporting the best is one search with 2,000 attempts, not 2,000 discoveries.

## 3. The ledger

`strategy_attempts.sqlite3` (Git-ignored) holds one row per
(variant, symbol, timeframe, dataset):

* the parameters and the **grid version** that produced them;
* the metric payload from the same evaluator the paper lane uses;
* the verdict, its reason, and every check with the number behind it;
* the bootstrap p-value, the FDR q-value and the Reality Check p-value.

A rejected attempt stays in the ledger. Deleting failures is how a search turns into
a story about one lucky combination. `strategy_selection` holds the operator's
choice and the attempt it came from.

## 4. Evaluation: the same engine, the same splits

`library/evaluation.evaluate_variant()` wraps the existing
`paper_runner.evaluation.evaluate_candidate()`. A variant becomes a `Candidate` and
is scored with:

* the chronological 60/20/20 split and the walk-forward folds;
* fills at the next bar's ask/bid, the imported spread, the platform's commission,
  slippage and swap;
* a locked test window that is spent **once** per dataset;
* the paper lane's own criteria (≥30 closed trades, ≤10% drawdown, ≥3 folds, ≥60%
  positive folds, positive validation and locked test).

There is no second backtest engine. Two engines disagreeing about the same bars is
how a strategy gets "validated" twice and believed once.

A timeframe the template does not declare is refused
(`timeframe_not_declared`), and so is a dataset with no declared data class.

## 5. Multiple testing, honestly

Three numbers, each answering a different question:

| Control | Question |
| --- | --- |
| Block-bootstrap p-value | How often would noise look this good? |
| Benjamini–Hochberg `q` | With *m* variants tried, does this one survive the count? |
| White's Reality Check | Does the **best** variant beat luck once every attempt is counted? |

Defaults: FDR `q ≤ 0.10`, Reality Check `p ≤ 0.05`, 1,000 resamples, block length 5,
fixed seed. A series with fewer than 3 observations cannot be tested, and the screen
then says so instead of inventing a p-value.

These tests control selection bias; they do not remove it, they do not include the
cost of the search, and **statistical significance is not profitability**.

## 6. The screen

Predeclared, versioned (`strategy-library-criteria-v1`), and printed on the page:

* the dataset's data class must be able to promote
  (`USER_SUPPLIED_HISTORICAL`, `USER_SUPPLIED_PRODUCTION`); a fixture or demo can be
  studied but can never make a variant eligible;
* ≥30 closed trades in the locked test;
* max drawdown ≤10%;
* ≥3 walk-forward folds, ≥60% of them positive;
* positive validation and positive locked test;
* `q ≤ 0.10` and Reality Check `p ≤ 0.05`;
* no risk-gate rejections inside the locked test window (reported, not blocking).

Outcomes: `ELIGIBLE`, `REJECTED`, or `INSUFFICIENT_EVIDENCE`. Each comes with a
sentence long enough to be checked and short enough to read.

## 7. Selection: the operator's, never the system's

* Only an **eligible** attempt can be selected. There is no override flag.
* Selecting requires an explicit confirmation checkbox and records the attempt
  (dataset, symbol, timeframe) — not a verdict that the strategy is good.
* A selection is a label on a ledger row. It does not size, arm, place, or change a
  mode.
* Selected variants appear in the live plan's candidate list as
  `library::<variant_id>` — with their evidence attached, so the live page labels
  them honestly. Reaching an order still requires: choosing it in the live plan,
  reading the review, typing the confirmation phrase, the risk gate agreeing, a
  broker-side protective stop, and the PAPER/LIVE mode the operator chose.
* A selection made while the workstation is running is picked up by the live plan
  the next time the process starts; the running live session is never reconfigured
  underneath the operator.

## 8. Monitoring

After setup the page shows, per symbol and timeframe, how many templates declare that
timeframe and how many bars are available. A timeframe **no template declares is
never analysed** — a bar feed is not evidence that a strategy supports intraday or
second-level decisions. Combinations with too few bars are listed with that reason.

Monitoring scores candidates; it never promotes one. Promotion is an operator
action.

## 9. What this is not

* Not a promise of returns, and not a ranking of "best strategies".
* Not a claim that a cited paper transfers to this broker, this spread, or this
  decade.
* Not a replacement for the live risk controls: account-level limits, per-position
  sizing, the protective stop requirement, and the reconciliation on restart are all
  in the live path and untouched by this library.
* Tick and second-level timeframes are absent from every card. Working at that
  resolution needs a measured execution latency budget, a tick feed, and a cost model
  that survives it; none of that is claimed here.
