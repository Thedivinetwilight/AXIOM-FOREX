# Multi-pair research evaluation (offline, imported data only)

`axiom_forex/evaluation/` evaluates **several pairs at once** on a shared
multi-currency book, using only canonical imported datasets. It is offline and
research-only: it makes no network call, reads no credential, opens no account
connection, creates no order, never touches the paper order stream, and never
enables external execution or deployment.

Every result is labelled with the dataset class and provenance, and is a
historical simulation. Nothing in this workflow is a profitability claim or
evidence of live readiness.

- Report format: `AXIOM_FOREX_RESEARCH_EVALUATION` (schema `1.0.0`)
- Mode: `RESEARCH_ONLY`
- Evaluable data classes: `USER_SUPPLIED_DEMO`, `USER_SUPPLIED_HISTORICAL`,
  `USER_SUPPLIED_PRODUCTION`
- Qualifiable data classes: `USER_SUPPLIED_HISTORICAL`,
  `USER_SUPPLIED_PRODUCTION` only
- Activation: never. `QUALIFICATION_NEVER_ACTIVATES` is part of every report.

## Why a separate workflow

The paper workflow is single-pair, fixture-backed, and built to produce a
durable order/fill stream. The evaluation workflow is the opposite shape: many
pairs, imported datasets, no orders at all. It reuses the reviewed building
blocks (`validation.split`, `validation.walk_forward`, `portfolio.costs`,
`accounting.fx`, `risk.aggregate`, `regimes.session`, `indicators`,
`strategies`) behind a new ledger and engine so that neither path can leak into
the other.

| Concern | Paper workflow | Research evaluation |
| --- | --- | --- |
| Instruments | one fixture pair | 2+ imported pairs |
| Data class | `SYNTHETIC_FIXTURE` | `USER_SUPPLIED_*` |
| Ledger | single-currency `ForexPortfolio` | `MultiPairBook` (per-currency cash) |
| Orders | `LocalPaperBroker` | none (in-memory fills only) |
| Activation | gated fixture activation | never |

## Pipeline

1. **Config validation** (`EvaluationConfig.validate`). Fractions must sum to
   one, walk-forward sizes must be positive, thresholds must be explicit. Cost
   terms, conversion rates, rollover policy, swap cards, correlation matrix, and
   aggregate limits are injected; nothing is defaulted.
2. **Alignment** (`alignment.build_alignment`). Every pair/timeframe is projected
   onto one UTC grid. For each pair and slot the state is `COVERED`,
   `MISSING_WHILE_OPEN`, or `CLOSED_BY_CALENDAR`, decided by the pair's own
   trading calendar (`calendar.status(pair, at)`, including daily breaks,
   holidays, DST, and validity bounds). Missing runs and closed runs are
   compressed into explicit intervals. Bars stamped inside a declared closure are
   counted as `out_of_session_bars`; no missing price is ever interpolated.
3. **Eligibility** (`eligibility.evaluate_eligibility`). Fail-closed gates for:
   synthetic fixtures, unrecognised or mixed data classes, failed import
   manifests, too few pairs, too few bars, unusable data quality, coverage below
   the declared threshold, out-of-session bars, and calendar/correlation terms
   that do not cover a pair. A failing gate returns a `BLOCKED` report with the
   reason codes; no phase runs.
   The gate also **re-validates every bar it is about to evaluate** instead of
   trusting `PairSeries.quality`, which is only an import-time label:
   `bar.validate(spec)` (`invalid_bar`), series timeframe/duration agreement
   (`bar_timeframe_mismatch`), monotonic non-overlapping ordering
   (`bars_out_of_order`), and agreement between the declared data class and the
   `import_manifest`/`dataset_manifest` it cites
   (`data_class_provenance_mismatch`). Findings are capped at five per code with
   a summary count so a corrupted file cannot flood the report. A stale quote
   stamp, an incomplete bar, a bar carrying another pair, a duplicate, a
   reversed pair of bars, a crossed (bid > ask) bar, or a relabelled data class
   therefore produces a `BLOCKED` report with no phases rather than a `PASS`.
4. **Chronological split** (`splits.chronological_plan`, reusing
   `axiom_forex.validation.split`). Train, validation, and test windows are
   half-open, contiguous, and mutually disjoint.
5. **Walk-forward** (`splits.walk_forward_plans`, reusing
   `axiom_forex.validation.walk_forward`). Rolling folds live **inside the
   development (train) segment only**. Each fold's test window is checked against
   both held-out windows, so the validation and final test sets are evaluated
   once each and never re-tested.
6. **Execution simulation** (`engine.run_phase`). Decisions use only completed
   bars up to the decision bar. Fills happen at the **next** same-pair bar open:
   ask for a buy, bid for a sell. A fill that would need the decision bar itself
   raises `LookAheadError`. A fill in a closed calendar slot is refused
   (`FILL_OUTSIDE_CALENDAR_SESSION`). The final bar's signal is cancelled and
   counted rather than filled with incomplete information.
7. **Portfolio accounting** (`book.MultiPairBook`). Cash is held per currency and
   converted with the explicit rate book. An open position is valued at **its own
   liquidation mark**, not at the static conversion cross: the currency legs and
   the pair price are independent declared inputs, so mixing them would make
   equity jump by however far an entry price sits from the implied cross and then
   jump back when the position closes. Equity is therefore cash plus realized and
   unrealized P&L, and it agrees with the per-trade table. Exposure is aggregated
   per **shared currency**, not per pair, then checked against
   correlation-adjusted aggregate limits before every entry. A missing mark
   raises `MissingMarkError` instead of guessing a price. Costs are the existing
   commission, slippage, and rollover terms, applied on entry and exit; a trade's
   net P&L subtracts **both** sides, so `gross - costs == net` holds for every
   trade and for every group that sums trades.
   Financing accrues only at rollover cutoffs on days the pair's calendar
   actually trades: a weekend or holiday cutoff is skipped rather than being
   charged *and* covered by the configured triple-rollover weekday. Financing is
   settled when a position closes (cash basis): a position that is still open at
   a phase boundary reports unrealized price P&L only, not an accrued swap, so a
   multi-day open position's reported cost is a lower bound until it closes.
8. **Metrics** (`metrics`). Pair, session, regime, and portfolio grouping with
   trades, wins/losses, gross/net P&L, costs, turnover, exposure, drawdown, and
   data-quality coverage. Group drawdown is the realized-P&L drawdown of that
   group's trade sequence; the portfolio drawdown comes from the equity curve.
   Turnover is two-sided traded notional (entry plus exit) throughout.
9. **Qualification** (`research`). A research-only decision listing explicit
   reasons (`insufficient_walk_forward_folds`, `insufficient_trades`,
   `positive_fold_ratio_below_threshold`, `drawdown_exceeds_threshold`,
   `data_class_not_qualifiable`, `no_conclusive_walk_forward_folds`). It can
   never activate a strategy and never edits lifecycle state.

Headline metrics use **held-out evidence only** (the chronological validation and
test phases). Walk-forward fold results are reported separately so development
and held-out numbers are never mixed into one figure.

## Candidate selection

`axiom_forex.tournament.TournamentRunner` evaluates candidates chronologically
and **selects on the validation segment only**. The locked test segment is opened
once, its P&L is reported as `oos_pnl_account`, and it is required to be positive
for a candidate to qualify as evidence -- but it is never the ranking key
(`selection_basis="VALIDATION"`). Selecting the candidate that happened to score
best on the locked test would make that window part of the search, so its P&L
could no longer be read as out-of-sample evidence.

## Test-window custody

`TestWindowRegistry` records every consumed out-of-sample window keyed by a
dataset fingerprint and its bounds. Reserving the same window again, or any
**overlapping** window for the same dataset (for example a walk-forward step
smaller than the test size), raises `TestWindowReuseError`. An `EvaluationSet`
keeps its registry, so running it twice fails closed instead of silently
re-testing.

## Report contents

`render_evaluation_markdown` appends a section to the platform report:

- evaluation id, mode, status, reason, and data class;
- aligned grid with all-pairs-open, partly-open, and closed slot counts;
- per-pair dataset table with data class, source, source version, bar count,
  coverage ratio, and manifest validity;
- data-eligibility findings with reason codes;
- chronological split windows and walk-forward fold table;
- performance and risk by pair, session, and regime;
- portfolio costs, turnover, drawdown, and exposure;
- qualification status, reasons, and the never-activates note.

Provenance values are labels, versions, and hashes. Audit payloads pass through
`sanitize_audit_value`, which redacts secret-like keys and URLs; callers do not
put local filesystem paths into reports. The configured SQLite location is
reported by `/status`, `/health`, and the desktop view as `:memory:` or a generic
`<local-database-file>` marker, never as an absolute path. A regression test
scans every operator surface for the resolved path and the API token.

## Stress and sensitivity suite

`axiom_forex.evaluation.stress` reruns the **same** pinned evaluation under
explicitly configured adverse assumptions. It is a sensitivity measurement on top
of the imported-data evaluation, not a second strategy, a paper run, or a
qualification path.

Scenarios are always supplied by the operator; nothing is invented or defaulted:

```python
from axiom_forex.evaluation import load_stress_scenarios, run_stress_suite

scenarios = load_stress_scenarios("fixtures/stress_scenarios.json")
suite = run_stress_suite(
    series=series, config=config, calendar=calendar,
    scenarios=scenarios, baseline=baseline_report, now=now,
)
```

Supported kinds (strict per-kind parameters, no hidden defaults):

| Kind | Parameters |
| --- | --- |
| `SPREAD_WIDENING` | `extra_spread_pips` **or** `spread_multiplier` |
| `SLIPPAGE_INCREASE` | `extra_slippage_pips` **or** `slippage_multiplier` |
| `COMMISSION_INCREASE` | `extra_commission_rate` **or** `commission_multiplier` |
| `ADVERSE_SWAP` | `swap_rate_multiplier` |
| `QUOTE_DELAY` | `delay_seconds`, `max_staleness_seconds`, `pairs` |
| `MISSING_QUOTES` | `drop_every_nth` (`>= 2`), optional `pairs` |
| `WEEKEND_GAP` | `gap_start`, `gap_end` (ISO-8601 UTC), `pairs` |
| `CONVERSION_SHOCK` | `rate_shocks` (`{"FROM/TO": multiplier}`) |
| `CORRELATION_STRESS` | `correlation_overrides` (`{"PAIR": -1..1}`) |

Provenance rules:

- every scenario declares an `evidence_class`: `HYPOTHETICAL_ASSUMPTION`,
  `IMPORTED_DATA_BACKED`, or `DOCUMENTED_PROVIDER_TERMS`;
- the latter two require a non-empty `evidence_reference`; a hypothetical
  scenario requires a description. Fixture values are never presented as
  provider facts, and no "realistic" shock size is guessed;
- derived inputs (widened bars, shortened series) keep the imported dataset
  hash (`derived_from_dataset_sha256`) and add
  `stress_scenario_id` / `stress_derivation`;
- the report lists the evidence class, the reference, and every parameter next
  to the observed (imported) baseline it was applied to.

Isolation guarantees:

- strategy parameters, quantities, cost terms other than the declared one,
  calendar, and split are unchanged; `apply_scenario` returns fresh objects
  instead of mutating the baseline inputs;
- the baseline plan and folds are reused (`fixed_plan`/`fixed_folds`), and the
  run sets `selection=False`, so **no out-of-sample window is claimed or
  consumed** and no test-window reservation is attempted;
- every run is labeled `purpose="STRESS:<scenario_id>"`,
  `selection_allowed=False`, and qualification is forced to `SENSITIVITY_ONLY`
  with reason `stress_scenario_not_qualifying`; activation stays impossible;
- a scenario that removes data fails closed through the existing alignment,
  coverage, and bar-count gates (`BLOCKED` with explicit reasons) rather than
  interpolating, back-filling, or relaxing thresholds;
- missing, unknown, conflicting, no-effect, or out-of-range parameters raise
  `StressScenarioError` before any evaluation runs;
- conversion shocks and correlation overrides are keyed by **canonical** legs and
  pairs: a malformed key such as `AAA/BBB/CCC` is refused by validation (and by
  the runner) instead of crashing with a bare `ValueError`, an override for a
  pair that does not exist is an error rather than a silent no-op, and two
  spellings of one leg (`AAA/BBB` and `aaa/bbb`) are refused instead of letting
  dict order decide which multiplier applies.

Comparisons are produced per portfolio and per pair for trades, win ratio, gross
P&L, costs, net P&L, turnover, drawdown, exposure, margin, coverage, and
rejection count. Stress results are hypothetical reconstructions and are not
evidence of profitability or live readiness.

## Operator surfaces

- `ForexPlatform.research_evaluation(config=None, calendar=None)` runs the
  workflow over the imported datasets and returns the report.
- `ForexPlatform.research_evaluation_summary()` is the compact view returned by
  `/evaluation`, and by `status()` under `research_evaluation`.
- `ForexPlatform.report_markdown()` appends the evaluation section when one has
  been run.
- `verify()` includes `research_evaluation_isolated`, which asserts no imported
  dataset is execution eligible and every returned report keeps
  `execution_isolation` all-`False`.
- CLI: `python -m axiom_forex.cli evaluate --import-paths a.csv,b.csv
  --import-pairs AAA/BBB,AAA/CCC --import-data-class USER_SUPPLIED_DEMO ...`,
  optionally with `--evaluate-markdown` and `--evaluate-quantity`.
- `ForexPlatform.research_evaluation_stress(scenarios, ...)` runs the suite over
  the imported datasets; `research_evaluation_stress_summary()` reports
  `NOT_RUN` until a suite is run and is embedded under `stress` in
  `research_evaluation_summary()` (and therefore in `/evaluation`).
  `research_evaluation_stress_markdown()` renders the stress section into
  `report_markdown()`.
- CLI: `evaluate --stress-scenarios path/to/scenarios.json` loads an explicit
  scenario document (see `fixtures/stress_scenarios.json`); `--evaluate-markdown`
  then prints the evaluation **and** the stress section.
- `verify()` includes `stress_results_non_qualifying`, which asserts that any
  recorded stress run never qualifies, never activates, and creates no orders.

The fixture platform is single-pair, so a CLI evaluation over one pair returns a
`BLOCKED` report with `insufficient_pairs`. That is the intended behaviour: a
multi-pair evaluation needs a catalog, calendar, correlation matrix, and
aggregate limits that actually declare those pairs. Overriding the calendar,
correlation matrix, or limits is an explicit operator decision, not a default.

## Fixtures used by the tests

- `fixtures/multi_pair.py` declares three instrument/calendar fixtures whose
  currencies overlap (`AAA/BBB`, `AAA/CCC`, `BBB/CCC`, account currency `CCC`)
  with asynchronous sessions: each pair has its own daily break, and one closes
  earlier in the day. It also provides a triangular-consistent conversion-rate
  book, a correlation matrix, and aggregate limits. Synthetic data is never used
  as evaluation data: the tests import canonical CSV text and declare a
  `USER_SUPPLIED_*` class.
- `fixtures/canonical_csv.py` builds deterministic canonical CSV rows (bars plus
  quotes) so imports and evaluation stay reproducible.
- `fixtures/stress_scenarios.json` is an explicit **hypothetical** scenario
  document used by the stress tests. It states in its own header that none of
  its parameters is a documented provider term, a market observation, or a
  recommendation; replacing it with provider-backed values is an operator
  decision that requires a reference.

## Deterministic tests

`tests/test_forex_research_evaluation.py` covers pair alignment and explicit
missing intervals, asynchronous sessions (partly-open and closed slots), currency
conversion of notional and P&L, shared-currency exposure aggregation, correlated
exposure rejection, missing-mark failure, chronological and walk-forward
isolation, test-window reuse, no-look-ahead fills, data-class eligibility
(demo/historical/synthetic/mixed/production), insufficient bars, incomplete
coverage, out-of-session bars, missing calendar/correlation terms, reporting
labels, metric grouping, determinism, and the operator surfaces.

`tests/test_forex_stress_scenarios.py` covers scenario validation and
provenance labels, parameter typos and conflicting/no-effect parameters, spread,
slippage, commission, and swap application, delayed/missing quotes and weekend
gaps (fail-closed plus derived-series provenance), conversion and correlation
shocks, baseline-plan reuse without claiming a window, `SENSITIVITY_ONLY`
non-qualification, determinism, invalid scenario documents, and the operator
surfaces.

## What still depends on a selected data provider or broker

- The multi-pair **instrument catalog**: spec version, pip/tick/precision,
  contract size, min/step/max quantity, margin multiplier, correlation group per
  pair.
- The **trading calendar** per pair: sessions, daily breaks, holidays, DST rules,
  validity bounds, and instrument availability. No calendar is inferred.
- **Conversion rates** for every currency leg. Rates must be internally
  consistent: an inconsistent triangle makes an otherwise flat position look
  profitable or loss-making.
- **Cost terms**: commission, slippage, financing/swap rates, rollover cutoffs
  and the triple-rollover rule.
- **Correlation matrix** and **aggregate limits** used for correlated-exposure
  checks.
- **Session buckets** used in reporting. The default UTC buckets
  (`DEFAULT_RESEARCH_SESSIONS`) are analysis windows for grouping, not broker
  sessions.
- **History depth and quality policy**: how many bars are required per pair, the
  accepted coverage ratio, and how gaps/corrections are reported.
- **Realistic stress sizes**: typical/peak spreads per session, observed slippage
  distributions, published commission and swap schedules, conversion-rate
  volatility, and correlation regimes. Every scenario above is a declared
  assumption until those are imported or documented with a reference; the
  suite never substitutes a guessed "realistic" value.
- A **vendor export adapter** to produce the canonical CSV; none is included.

Until those are supplied from official documentation and reviewed, results stay
labelled `USER_SUPPLIED_*`, qualification can only ever be "for research review",
and activation remains impossible.
