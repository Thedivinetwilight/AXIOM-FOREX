# AX-SCALPER lab report

Generated: 2026-11-09T05:16:44.087137+00:00

> These are measurements on synthetic ticks with declared costs. They are not a profit claim, not a forecast, and not evidence that any strategy has an edge in a live market.

>  Read the funnel and the refusals before the expectancy: the scenarios are deterministic synthetic paths, and a persistent drift is the most favourable possible world for a momentum rule. A large expectancy here measures the generator as much as the strategy.

## What was run

| Scenario | Data class | Ticks | Injected | Digest |
| --- | --- | --- | --- | --- |
| trend_up_16_shifted | SAMPLE_DATA | 400 | — | `6a2492b9a7d0a228` |
| ranging_shifted | SAMPLE_DATA | 400 | — | `a34cbe7c6ff86513` |
| breakout_shifted | SAMPLE_DATA | 400 | — | `d4e53aa88edfc8e3` |
| reversal_shifted | SAMPLE_DATA | 400 | trend_reversal | `dfbec80906c4d1cb` |
| spread_expansion_shifted | SAMPLE_DATA | 300 | spread_expansion | `d1b910fe516f437e` |
| intermittent_feed_shifted | SAMPLE_DATA | 260 | tick_gap | `ee3146d6773cc091` |
| dirty_feed_shifted | SAMPLE_DATA | 324 | duplicates, out_of_order, repeated_quotes | `2f810ada0433bec3` |
| volatile_shifted | SAMPLE_DATA | 400 | high_volatility | `518abe721f490e86` |

## Training pass

219 simulated brackets over the training replay; median spread 8.0 points.

> Training outcomes are measured on the training replay only. They are used to calibrate probabilities, never to report performance.

| Fingerprint / regime | Wins | Losses |
| --- | --- | --- |
| `1184e364|BREAKOUT` | 13 | 0 |
| `a74d2a6c|BREAKOUT` | 13 | 0 |
| `a74d2a6c|TRENDING_DOWN` | 7 | 0 |
| `a74d2a6c|TRENDING_UP` | 178 | 8 |

## The funnel

| Scenario | Ticks | Opportunities | Signals | EV accept | EV reject | Sized | Fills | Exits |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| trend_up_16_shifted | 400 | 1280 | 256 | 256 | 0 | 15 | 15 | 15 |
| ranging_shifted | 400 | 1805 | 0 | 0 | 0 | 0 | 0 | 0 |
| breakout_shifted | 400 | 1035 | 45 | 45 | 0 | 5 | 5 | 5 |
| reversal_shifted | 400 | 1050 | 178 | 178 | 0 | 18 | 18 | 18 |
| spread_expansion_shifted | 300 | 665 | 266 | 133 | 133 | 4 | 4 | 4 |
| intermittent_feed_shifted | 260 | 235 | 69 | 47 | 22 | 2 | 2 | 2 |
| dirty_feed_shifted | 320 | 1160 | 310 | 192 | 118 | 7 | 7 | 7 |
| volatile_shifted | 400 | 1805 | 0 | 0 | 0 | 0 | 0 | 0 |

## Performance, after costs

| Scenario | Trades | Net expectancy | Std dev | Profit factor | Win rate | Avg win | Avg loss | Expectancy R | Sharpe | Sortino | Max DD (points) | Cost / gross |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| trend_up_16_shifted | 15 | 19.4871 | 1.4734 | None | 1.0 | 19.4871 | None | None | 13.2259 | inf | 0.0 | 0.3391 |
| ranging_shifted | 0 | 0.0 | 0.0 | None | 0.0 | None | None | None | None | None | 0.0 | None |
| breakout_shifted | 5 | 15.8939 | 8.918 | 1694.2084 | 0.8 | 19.8792 | -0.0469 | 423.5521 | 1.7822 | 338.6417 | 0.0469 | 0.3862 |
| reversal_shifted | 18 | 16.8606 | 13.8819 | 9.0925 | 0.9444 | 20.0584 | -37.5027 | 0.5349 | 1.2146 | 0.4496 | 37.5027 | 0.3723 |
| spread_expansion_shifted | 4 | 11.9863 | 10.8509 | 15.4807 | 0.75 | 17.0854 | -3.311 | 5.1602 | 1.1046 | 3.6202 | 3.311 | 0.5387 |
| intermittent_feed_shifted | 2 | 32.2831 | 17.8461 | None | 1.0 | 32.2831 | None | None | None | None | 0.0 | 0.2365 |
| dirty_feed_shifted | 7 | 15.7625 | 10.0126 | 16.9032 | 0.8571 | 19.5459 | -6.9381 | 2.8172 | 1.5743 | 2.2719 | 6.9381 | 0.3882 |
| volatile_shifted | 0 | 0.0 | 0.0 | None | 0.0 | None | None | None | None | None | 0.0 | None |

Sharpe and Sortino here are per-trade mean divided by per-trade dispersion, not annualised. Max drawdown is in points, not in account currency, and it is measured on the trade sequence, not on an intra-trade equity curve.

## Costs, spreads, latency and holding time

### trend_up_16_shifted

| Measure | n | mean | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| spread_points_at_entry | 15 | 8.0 | 8.0 | 8.0 | 8.0 |
| cost_points | 15 | 10.0 | 10.0 | 10.0 | 10.0 |
| slippage_points | 15 | 1.0 | 1.0 | 1.0 | 1.0 |
| fill_latency_ms | 15 | 16.2 | 13.0 | 34.0 | 37.0 |
| holding_ms | 15 | 4800.0 | 4800.0 | 5200.0 | 5200.0 |
| mae_points | 15 | -7.3922 | -7.38572453 | -7.23201612 | -7.22909428 |
| mfe_points | 15 | 29.4871 | 29.78419403 | 30.38381439 | 30.51944998 |
| r_multiple | 15 | 0.7495 | 0.7609305396153846 | 0.7839928611538461 | 0.7892096146153846 |

### ranging_shifted

| Measure | n | mean | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |

### breakout_shifted

| Measure | n | mean | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| spread_points_at_entry | 5 | 8.0 | 8.0 | 8.0 | 8.0 |
| cost_points | 5 | 10.0 | 10.0 | 10.0 | 10.0 |
| slippage_points | 5 | 1.0 | 1.0 | 1.0 | 1.0 |
| fill_latency_ms | 5 | 12.2 | 9.0 | 25.0 | 25.0 |
| holding_ms | 5 | 3160.0 | 3400.0 | 3600.0 | 3600.0 |
| mae_points | 5 | -6.7974 | -6.80735708 | -6.69261845 | -6.69261845 |
| mfe_points | 5 | 25.8939 | 29.72125668 | 30.39582207 | 30.39582207 |
| r_multiple | 5 | 0.6113 | 0.7585098723076923 | 0.784454695 | 0.784454695 |

### reversal_shifted

| Measure | n | mean | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| spread_points_at_entry | 18 | 8.0 | 8.0 | 8.0 | 8.0 |
| cost_points | 18 | 10.0 | 10.0 | 10.0 | 10.0 |
| slippage_points | 18 | 1.0 | 1.0 | 1.0 | 1.0 |
| fill_latency_ms | 18 | 16.0 | 13.0 | 34.0 | 37.0 |
| holding_ms | 18 | 3244.4444 | 2600.0 | 4600.0 | 4600.0 |
| mae_points | 18 | -7.5503 | -6.06643477 | -5.51528253 | -5.51508736 |
| mfe_points | 18 | 28.5284 | 30.48798208 | 31.66712008 | 31.92876811 |
| r_multiple | 18 | 0.6485 | 0.7879993107692308 | 0.8333507723076923 | 0.8434141580769231 |

### spread_expansion_shifted

| Measure | n | mean | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| spread_points_at_entry | 4 | 12.0 | 16.0 | 16.0 | 16.0 |
| cost_points | 4 | 14.0 | 18.0 | 18.0 | 18.0 |
| slippage_points | 4 | 1.0 | 1.0 | 1.0 | 1.0 |
| fill_latency_ms | 4 | 14.25 | 20.0 | 25.0 | 25.0 |
| holding_ms | 4 | 11050.0 | 11800.0 | 14800.0 | 14800.0 |
| mae_points | 4 | -14.4942 | -16.08447113 | -8.08554028 | -8.08554028 |
| mfe_points | 4 | 25.9863 | 29.84920528 | 29.91580151 | 29.91580151 |
| r_multiple | 4 | 0.461 | 0.74965935 | 0.7659923657692308 | 0.7659923657692308 |

### intermittent_feed_shifted

| Measure | n | mean | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| spread_points_at_entry | 2 | 8.0 | 8.0 | 8.0 | 8.0 |
| cost_points | 2 | 10.0 | 10.0 | 10.0 | 10.0 |
| slippage_points | 2 | 1.0 | 1.0 | 1.0 | 1.0 |
| fill_latency_ms | 2 | 14.5 | 9.0 | 20.0 | 20.0 |
| holding_ms | 2 | 126100.0 | 7800.0 | 244400.0 | 244400.0 |
| mae_points | 2 | -7.9273 | -7.96903036 | -7.88554028 | -7.88554028 |
| mfe_points | 2 | 42.2831 | 29.66399154 | 54.90211872 | 54.90211872 |
| r_multiple | 2 | 1.2417 | 0.7563073669230769 | 1.7270045661538462 | 1.7270045661538462 |

### dirty_feed_shifted

| Measure | n | mean | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| spread_points_at_entry | 7 | 8.0 | 8.0 | 8.0 | 8.0 |
| cost_points | 7 | 10.0 | 10.0 | 10.0 | 10.0 |
| slippage_points | 7 | 1.0 | 1.0 | 1.0 | 1.0 |
| fill_latency_ms | 7 | 14.4286 | 9.0 | 34.0 | 34.0 |
| holding_ms | 7 | 6942.8571 | 7600.0 | 7800.0 | 7800.0 |
| mae_points | 7 | -8.0026 | -7.96903036 | -7.88489658 | -7.88489658 |
| mfe_points | 7 | 25.7625 | 29.6593697 | 29.75124759 | 29.75124759 |
| r_multiple | 7 | 0.6062 | 0.7561296038461538 | 0.7596633688461538 | 0.7596633688461538 |

### volatile_shifted

| Measure | n | mean | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |

## By regime and by session

### trend_up_16_shifted

**regime**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| TRENDING_UP | 15 | 19.4871 | 1.0 | None |

**session**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| asia | 15 | 19.4871 | 1.0 | None |

### ranging_shifted

### breakout_shifted

**regime**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| TRENDING_UP | 5 | 15.8939 | 0.8 | 1694.2084 |

**session**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| asia | 5 | 15.8939 | 0.8 | 1694.2084 |

### reversal_shifted

**regime**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| BREAKOUT | 4 | 17.8296 | 1.0 | None |
| TRENDING_DOWN | 2 | 20.5601 | 1.0 | None |
| TRENDING_UP | 12 | 15.921 | 0.9167 | 6.0944 |

**session**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| asia | 18 | 16.8606 | 0.9444 | 9.0925 |

### spread_expansion_shifted

**regime**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| TRENDING_UP | 4 | 11.9863 | 0.75 | 15.4807 |

**session**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| asia | 4 | 11.9863 | 0.75 | 15.4807 |

### intermittent_feed_shifted

**regime**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| TRENDING_UP | 2 | 32.2831 | 1.0 | None |

**session**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| asia | 2 | 32.2831 | 1.0 | None |

### dirty_feed_shifted

**regime**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| TRENDING_UP | 7 | 15.7625 | 0.8571 | 16.9032 |

**session**

| Bucket | n | Mean net | Win rate | Profit factor |
| --- | --- | --- | --- | --- |
| asia | 7 | 15.7625 | 0.8571 | 16.9032 |

### volatile_shifted

## Independent validation

| Strategy | Trades | OOS trades | PBO | Deflated Sharpe | Cost break | Approved | Findings |
| --- | --- | --- | --- | --- | --- | --- | --- |
| momentum_continuation | 51 | 13 | None | 0.997071 | 3.0 | no | probability_of_backtest_overfitting_too_high: cannot be computed; not_enough_out_of_sample_trades: 13 < 60; not_enough_trades: 51 < 200 |

### momentum_continuation

- Confidence interval on mean net points: {'low': 14.232479, 'high': 19.999657, 'level': 0.95, 'resamples': 1000, 'includes_zero': False, 'note': 'A bootstrap interval over trades, not a prediction interval for the next trade.'}
- Walk-forward: passes (4/4 consistent)
- Cost sensitivity: [{'cost_multiple': 1.0, 'mean_net_points': 17.6101, 'positive': True}, {'cost_multiple': 1.5, 'mean_net_points': 12.4533, 'positive': True}, {'cost_multiple': 2.0, 'mean_net_points': 7.2964, 'positive': True}, {'cost_multiple': 3.0, 'mean_net_points': -3.0173, 'positive': False}]
- Spread stress: [{'extra_spread_points': 0.0, 'mean_net_points': 17.6101}, {'extra_spread_points': 1.0, 'mean_net_points': 16.6101}, {'extra_spread_points': 2.0, 'mean_net_points': 15.6101}, {'extra_spread_points': 4.0, 'mean_net_points': 13.6101}]
- Criteria: {'version': 'skeptic-v1', 'min_trades': 200, 'min_out_of_sample_trades': 60, 'min_walk_forward_folds': 3, 'cost_multiple_survival': 1.5, 'max_pbo': 0.25, 'min_deflated_sharpe': 0.95, 'min_profit_factor': 1.1, 'require_positive_ci': True, 'q_value': 0.1, 'declared': 'Fixed in configuration before measurement. Adjusting these after a run, or retesting against the locked window until they pass, invalidates the result.'}

## Paper-forward comparison and degradation

The evaluation window is compared with a forward *replay* of the same price path at **twice the spread**. That isolates a cost regime change from a change in the price path. It is not a live paper-forward session: no real-time run has been made in this sandbox.

| Scenario | Baseline mean | Forward mean | Delta | Std error | Significant | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| trend_up_16_shifted | 19.4871 | 9.4082 | -10.0789 | 2.4204 | True | degraded |
| ranging_shifted | 0.0 | 0.0 | 0.0 | None | False | no_detectable_change |
| breakout_shifted | 15.8939 | 8.6174 | -7.2765 | 5.529 | False | no_detectable_change |
| reversal_shifted | 16.8606 | 7.7451 | -9.1155 | 5.045 | False | no_detectable_change |
| spread_expansion_shifted | 11.9863 | -29.7789 | -41.7652 | 41.433 | False | no_detectable_change |
| intermittent_feed_shifted | 32.2831 | 16.2831 | -16.0 | 13.5886 | False | no_detectable_change |
| dirty_feed_shifted | 15.7625 | 8.2831 | -7.4793 | 4.8174 | False | no_detectable_change |
| volatile_shifted | 0.0 | 0.0 | 0.0 | None | False | no_detectable_change |

> A drop is only called degradation when it exceeds twice the combined standard error. With samples this small, `no_detectable_change` is not evidence of stability; it is the absence of evidence either way.

## Verified here, unverified elsewhere

**Verified in this sandbox (fakes, replay, unit tests)**

- tick validation, de-duplication, ordering, staleness, gaps, clock reset and replay, on synthetic and file-based ticks
- regime classification and the no-trade refusals, with eligibility asserted per family
- signal rules for all five families, including the flow-data refusal
- Beta posteriors, calibration, EV rejection on incomplete costs, and sizing refusals, in unit tests with exact arithmetic
- guardian limits, entry suspension versus position management, and the emergency-stop plan
- paper venue idempotency, injected rejects, partial fills and ambiguous outcomes, with recovery by query only
- stop/target/break-even/trailing/time exits, protection verification and reconciliation, on a simulated venue
- hash-chained ledger, seal-based truncation detection, and the refusal to approve a strategy without independent evidence
- skeptic walk-forward, PBO, deflated Sharpe and cost stress on generated series
- the whole desk end to end on synthetic replays (this report)

**Not verified here**

- any MetaTrader 5 terminal: no connection, no symbol table, no tick stream, no account figures were read
- real spread, slippage, commission, swap or partial-fill behaviour at any broker
- the terminal's own tick delivery timing, so the ingest-latency figures below are process measurements against replay stamps
- live paper-forward trading in real time (browser display, keyboard shortcuts, session persistence across a real trading day)
- MQL5-side event handling: the Python polling benchmark is reported, but the event-driven alternative has not been proposed or measured

## Ledger

- **trend_up_16_shifted**: 2542 entries, chain ok=True, order requests without a result=0
- **ranging_shifted**: 1806 entries, chain ok=True, order requests without a result=0
- **breakout_shifted**: 1271 entries, chain ok=True, order requests without a result=0
- **reversal_shifted**: 1918 entries, chain ok=True, order requests without a result=0
- **spread_expansion_shifted**: 1888 entries, chain ok=True, order requests without a result=0
- **intermittent_feed_shifted**: 538 entries, chain ok=True, order requests without a result=0
- **dirty_feed_shifted**: 2508 entries, chain ok=True, order requests without a result=0
- **volatile_shifted**: 1806 entries, chain ok=True, order requests without a result=0

## Limitations

- synthetic ticks; no broker feed, no real spread distribution
- the paper venue fills every accepted order unless a fault is injected
- costs are the declared terms in the configuration, not a broker's schedule
- no live execution path is exercised or reachable
- Synthetic ticks with a declared drift/noise/no-spread model. They are not a broker feed and not a sample of any market.
- The generator's persistent drift is the single most favourable world for a momentum rule. A high win rate here measures the generator as much as the rule, which is why it is reported next to the funnel instead of being called an edge.
- The paper venue fills every accepted order unless a fault is injected; real partial-fill and rejection rates are unknown and are not assumed to be zero.
- Costs are the declared terms in the configuration (spread from the tick, one point of slippage per side, zero commission declared as assumed). A real broker's schedule is unverified.
- Latency is measured inside this process against the replay stamps. It is not network latency and not the terminal's delivery delay.
- Nothing here has been run against a MetaTrader 5 terminal. See the report's 'verified / unverified' section.

No claim of profitability, live readiness, or market-wide behaviour is made anywhere in this report. Figures are reproducible from the scenario digests above and the configuration in `axiom_forex/scalper/config.py`.
