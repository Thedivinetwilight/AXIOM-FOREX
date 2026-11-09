# AX-SCALPER — configuration reference

Every number the scalper desk uses is declared here before anything is measured.
Nothing in this package reads a value from a backtest result, and no running agent
can change a limit (see `docs/SCALPER_RISK_CONTROLS.md`).

`ScalperConfig` is frozen and validated. `build_config(...)` raises
`ScalperConfigError` with a stable `reason` if the configuration is not usable, so
a misconfigured desk cannot start, look healthy, and then refuse every trade for an
unstated reason.

## 1. Mode and the paper-only boundary

| Field | Default | Meaning |
| --- | --- | --- |
| `mode` | `PAPER` | Any other value is refused at construction with `scalper_is_paper_only`. |

Paper mode is enforced in **two** places, deliberately:

1. `ScalperConfig.validate()` refuses any mode but `PAPER`.
2. `Trader.__init__` raises `TradingBlocked` if handed a venue whose `mode` is not
   `PAPER`, and `ScalperDesk.__init__` refuses a non-paper configuration.

There is no live venue in this package. The MT5 order module
(`axiom_forex/brokers/mt5/orders.py`) is not imported anywhere under
`axiom_forex/scalper/`, and `tests/test_scalper_desk_and_lab.py` fails if an import
ever appears.

## 2. Instruments

`InstrumentSpec` holds what the broker reported, and nothing it did not:

| Field | Meaning |
| --- | --- |
| `symbol`, `pair` | Byte-exact symbol and `BASE/QUOTE` pair. `pair` is used for correlation, never inferred from the symbol name. |
| `point`, `digits` | Price increment and decimals. |
| `contract_size` | Units per lot. |
| `volume_min`, `volume_step`, `volume_max` | Broker volume terms. Size is floored to `volume_step`; it is never rounded up. |
| `stops_level_points` | Minimum stop distance. A signal closer than this is refused before submission with `stop_distance_inside_broker_stops_level`. |
| `margin_rate` | Margin as a fraction of notional. |
| `quote_currency`, `account_currency`, `conversion_rate` | Required to convert points into account currency. Without a conversion, `point_value_per_lot` is `None` and the instrument is *incomplete*, not zero-cost. |
| `source` | Where the terms came from. Fixtures say `SAMPLE_DECLARED_TERMS` so no report can present them as a broker's specification. |
| `defects` | Anything known to be wrong with the terms. A non-empty tuple makes the instrument unusable. |

`point_value_per_lot = contract_size × point × conversion_rate`. It is derived,
never typed.

## 3. Cost terms

`CostTerms` states, for each part of the round turn, whether the number is
observed, estimated, assumed zero, or unknown:

* `OBSERVED` — measured from the broker.
* `ESTIMATE` — a declared figure that is not a measurement.
* `ASSUMED_ZERO` — a declared zero. It is a decision, printed as one.
* `UNKNOWN` — not a number at all. The EV engine refuses with `cost_cannot_be_priced`
  and the configuration refuses to build.

| Field | Default | Meaning |
| --- | --- | --- |
| `commission_per_lot` | `0` | Account currency per round turn. |
| `commission_state` | `ASSUMED_ZERO` | Unknown commission is never silently zero. |
| `slippage_points` | `1` | **Per side.** A round turn pays it twice. |
| `slippage_state` | `ESTIMATE` | |
| `swap_state` | `UNKNOWN` | Swap is out of scope for a scalp held for minutes; it is declared unknown rather than pretended to be zero. |

## 4. Tick core

| Field | Default | Meaning |
| --- | --- | --- |
| `max_quote_age_ms` | `1500` | A quote older than this is refused (`stale_tick`) and the desk will not decide on it. |
| `max_spread_points` | `30` | Soft spread ceiling used by the tick core, the regime, and the strategy pre-check. |
| `spread_expansion_multiple` | `2.5` | A spread above this multiple of the window median raises `spread_expansion`. |
| `gap_threshold_ms` | `5000` | Silence longer than this raises `tick_gap`. |
| `clock_reset_threshold_ms` | `60000` | A backward stamp jump larger than this is a clock reset, not an out-of-order tick. It clears the ordering baseline and the price window and starts a resync period. |
| `resync_ticks` | `5` | Clean ticks required after a reset before the core reports itself usable again. Trading stays off during it. |
| `tick_window` | `240` | Ticks kept per symbol for regime and signal evaluation. |
| `min_window_ticks` | `40` | Below this the desk reports `insufficient_window` and does not decide. |
| `duplicate_window` | `2048` | How far back content identity is remembered. |

## 5. Decision

| Field | Default | Meaning |
| --- | --- | --- |
| `cooldown_ms` | `5000` | Minimum gap between entries. |
| `decision_latency_budget_ms` | `250` | Maximum signal-to-submission time the guardian will accept. |
| `min_edge_points` | `0.6` | Expected net value, in points, required **at the lower bound** of the uncertainty interval. |
| `uncertainty_z` | `1.0` | How many standard deviations of the probability estimate are subtracted before the floor is applied. |

## 6. Position management

| Field | Default | Meaning |
| --- | --- | --- |
| `max_open_positions` | `2` | |
| `max_hold_ms` | `900000` | 15 minutes. A position older than this is closed by `max_hold_reached`. |
| `break_even_trigger_points` | `8` | Move the stop to entry + one point once the position is this far in profit. |
| `trail_after_points` | `10` | Start trailing from here. |
| `trail_distance_points` | `6` | Trail distance behind the best price. The stop only ratchets. |

## 7. Sizing

| Field | Default | Meaning |
| --- | --- | --- |
| `risk_fraction` | `0.0025` | 0.25 % of equity per trade. Bounded to (0, 2 %]. |
| `max_loss_per_trade_account` | `50` | Absolute cap, applied alongside the fraction — the smaller wins. |
| `max_aggregate_risk_fraction` | `0.01` | 1 % of equity across all open positions. |
| `max_leverage` | `5` | Notional ceiling as a multiple of equity. |
| `max_currency_exposure_fraction` | `0.60` | One currency's notional as a share of the **leverage allowance** (equity × `max_leverage`). An equity-sized cap would refuse the first ordinary lot instead of constraining concentration. |
| `max_correlated_positions` | `2` | Positions correlating at or above the threshold in `GuardianLimits`. |

Sizing is `lots = permitted loss ÷ (stop points × point value per lot)`, floored to
the broker's volume step. See `docs/SCALPER_RISK_CONTROLS.md`.

## 8. Guardian

| Field | Default | Meaning |
| --- | --- | --- |
| `max_daily_loss_fraction` | `0.02` | 2 % of the day's opening equity. Suspends entries for the day. |
| `max_drawdown_fraction` | `0.05` | 5 % from peak equity. Suspends entries. |
| `max_loss_streak` | `4` | Consecutive losers before entries stop. |
| `max_spread_points_hard` | `45` | Above this, no entry at all, regardless of soft limits. |
| `max_slippage_points` | `6` | Observed slippage above this refuses new entries. Before the first fill the declared estimate is compared instead — see the risk-control doc for why that is not a loophole. |
| `max_fill_latency_ms` | `750` | Applies from the first observed fill. |

## 9. Desk

| Field | Default | Meaning |
| --- | --- | --- |
| `max_decisions_per_minute` | `60` | A rate limit on decisions, not on orders. |
| `state_version` | `1` | For future state snapshots. |

## 10. Validation rules

`ScalperConfig.validate()` returns every problem, not the first:

* mode is `PAPER`; at least one instrument; no duplicate symbols;
* every instrument passes `InstrumentSpec.validate()` (positive point, contract,
  volume, margin; a known conversion when the quote currency differs from the
  account currency; no declared defects);
* cost terms are valid and the commission is *known*;
* `max_spread_points_hard ≥ max_spread_points`;
* `tick_window ≥ min_window_ticks ≥ 10`;
* risk fractions inside conservative bounds (risk ≤ 2 %, aggregate ≤ 5 %, daily loss
  ≤ 10 %, drawdown ≤ 20 %);
* `uncertainty_z` in (0, 3].

## 11. Changing a limit

Limits live in configuration. An agent calling
`Guardian.request_limit_change(...)` receives `accepted: False` with
`limits_are_not_agent_configurable`, and the attempt is written to the ledger.
Applying a change means constructing a new `Guardian` from a new configuration —
an operator act, visible in the diff.
