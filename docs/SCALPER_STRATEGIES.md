# AX-SCALPER — strategy specifications

Five families, one rule each, all deterministic. Every strategy publishes its
entry rule, its exit rule, its rule version and a **fingerprint** — a SHA-256 over
its name, family, rule version, parameters and signal TTL. The fingerprint is the
identity that calibration, approvals and the ledger are keyed on: change one
parameter and yesterday's evidence no longer applies to today's code.

Nothing in this document is a claim that any rule makes money. The lab measures
these rules on synthetic data and the SKEPTIC refuses to approve them; that refusal
is reported in `docs/SCALPER_LAB_REPORT.md`.

## 0. Common behaviour

Before a family looks at a shape, four gates run, in this order, and every one of
them produces a recorded outcome when it refuses:

1. **Regime eligibility.** `regime.allows(family)` — a family that the current
   regime does not admit returns `not_eligible_in_this_regime` with the regime name.
2. **Spread.** A spread wider than the strategy's own `max_spread_points` returns
   `spread_too_wide_for_this_strategy`.
3. **Window length.** Fewer than `minimum_ticks` returns `not_enough_ticks`.
4. **Data.** A family that needs trade-volume direction and does not have it
   returns `trade_flow_data_unavailable` rather than guessing.

A signal carries: side, reference price (ask for a buy, bid for a sell), stop and
target **in points**, the tick id it was derived from, the regime, the rule version
and the fingerprint. A `Signal` whose stop cannot be priced at the instrument's
point has `stop_price = None`, and the trader refuses it
(`protective_orders_not_verified`).

Stops are never guessed. Each family's stop is either a parameter or
`max(parameter, volatility multiple × typical tick move)` — never a broker default.

## 1. Momentum continuation (`momentum`)

`rule_version v1` · parameters `lookback_ticks=8`, `min_move_points=3.0`,
`stop_points=10`, `target_points=14`, `volatility_stop_multiple=2.5`,
`max_spread_points=20`.

* **Entry.** Over the last `lookback_ticks` quotes the mid must travel at least
  `min_move_points` in one direction, and the final two steps must agree in sign.
  Buy the rising case, sell the falling one.
* **Exit.** Stop at `max(stop_points, volatility_stop_multiple × mean absolute
  step)` points; target at `target_points`; a momentum flip exits early; the
  desk's `max_hold_ms` closes it otherwise.
* **Regimes.** `TRENDING_UP`, `TRENDING_DOWN`, `BREAKOUT`.

## 2. Breakout continuation (`breakout`)

`rule_version v1` · parameters `window=40`, `buffer_points=2.0`,
`stop_points=12`, `target_points=18`, `max_spread_points=20`.

* **Entry.** The last mid must exceed the highest mid of the previous `window`
  quotes by `buffer_points`, or fall below the lowest by the same buffer.
* **Exit.** Stop and target in points; a return inside the range exits
  (`FALLBACK_RANGE`).
* **Regimes.** `BREAKOUT` (full size) and `HIGH_VOL` (half size).

## 3. Pullback continuation (`pullback`)

`rule_version v1` · parameters `fast_ticks=5`, `slow_ticks=20`,
`tolerance_points=2.0`, `stop_points=9`, `target_points=13`,
`max_spread_points=20`.

* **Entry.** The fast mean must be above the slow mean by at least
  `tolerance_points` (uptrend), the last mid must be within the tolerance of the
  fast mean (the pullback), and the last step must be up (the reclaim). The mirror
  applies for a downtrend.
* **Exit.** Stop and target in points; a fast/slow cross against the position exits
  (`TREND_BROKEN`).
* **Regimes.** `TRENDING_UP`, `TRENDING_DOWN`.

## 4. Mean reversion (`mean_reversion`)

`rule_version v1` · parameters `window=40`, `z_threshold=2.0`, `stop_points=10`,
`min_target_points=4`, `max_spread_points=20`.

* **Entry.** The last mid's z-score against the window must exceed
  `z_threshold`; sell the stretched-high case, buy the stretched-low one. A window
  with no dispersion returns `no_setup` rather than dividing by zero.
* **Exit.** Target the window mean, floored at `min_target_points`; stop at
  `stop_points`; `max_hold_ms` otherwise.
* **Regimes.** `RANGING` (full size), `LOW_VOL` (half size).

## 5. Order-flow momentum (`order_flow`)

`rule_version v1` · parameters `window=60`, `imbalance_threshold=0.62`,
`stop_points=10`, `target_points=12`, `max_spread_points=20`.

* **Data requirement.** At least 80 % of the window's ticks must carry a trade
  volume. A quote-only feed returns `trade_flow_data_unavailable`.
* **Entry.** The volume-weighted share of upticks (the tick rule) must be at least
  `imbalance_threshold` and the last step must be up; the mirror applies for
  selling.
* **Exit.** Stop and target in points; the share recrossing the midpoint exits
  (`FLOW_REVERSED`).
* **Regimes.** **None in v1.** See below — the family is implemented and
  measured, but no regime admits it, so it cannot trade.

### Why order flow is not admitted

`order_flow` is absent from the regime eligibility map on purpose. Two reasons,
both prior to any backtest:

1. A tick-rule imbalance over one broker's quotes is a *proxy*, and a proxy whose
   mapping to actual order flow is unverified. There is no independent dataset in
   this repository to check it against.
2. Nothing has been validated. Admitting a family because its rule is
   well-argued is exactly the failure mode the SKEPTIC exists to prevent.

The family stays in the code so its rules are reviewable and so the lab can
measure it honestly, and it stays out of `_ELIGIBILITY` so that absence — not a
boolean flag — is the refusal. If it is ever admitted, that is an explicit diff
plus a SKEPTIC approval artifact.

### What order flow here is, and is not

The tick rule applied to **one broker's quote stream** is a proxy for direction,
not a view of the global order book. Spot FX is decentralised; another broker's
feed would give another number. Every flow estimate carries
`BASIS_FEED_PROXY` and a caveat saying exactly that, and the desk never presents it
as market-wide buying or selling.

## 6. Regime eligibility matrix

| Regime | Momentum | Breakout | Pullback | Mean reversion | Order flow | Size |
| --- | --- | --- | --- | --- | --- | --- |
| `TRENDING_UP` | yes | no | yes | no | no | 1.0 |
| `TRENDING_DOWN` | yes | no | yes | no | no | 1.0 |
| `RANGING` | no | no | no | yes | no | 1.0 |
| `BREAKOUT` | yes | yes | no | no | no | 0.75 |
| `HIGH_VOL` | no | yes | no | no | no | 0.5 |
| `LOW_VOL` | no | no | no | yes | no | 0.5 |
| `LOW_LIQUIDITY` | **no** | **no** | **no** | **no** | **no** | 0 |
| `SPREAD_EXPANSION` | **no** | **no** | **no** | **no** | **no** | 0 |
| `ABNORMAL` | **no** | **no** | **no** | **no** | **no** | 0 |

The three no-trade regimes have **no entry in the eligibility map at all**. Absence
is the refusal: there is no code path by which they can become tradeable by
accident, and `RegimeState.size_multiplier` is `0` for them.

## 7. Why these parameters, and what would be dishonest

The defaults are declared starting points, not optima. They are deliberately
*narrow* relative to their costs: with an 8-point spread plus one point of slippage
per side, a round turn costs 10 points, and a 12-point stop with an 18-point target
cannot pay at any plausible win rate. The desk therefore refuses those brackets at
the EV gate, which is the correct behaviour and is visible in the lab funnel.

The lab does not "fix" that by searching for parameters that survive. It applies a
declared arithmetic screen — a target must clear `3 ×` the round-turn cost — and
then measures. That screen is a statement about arithmetic, not an optimisation:
below it, no win rate under 100 % pays.

## 8. Adding a strategy

A new family must:

1. declare `family`, `name`, `rule_version` and a frozen parameters dataclass;
2. implement `entry_rule`, `exit_rule`, `minimum_ticks` and `_evaluate`;
3. return a `Signal` with a positive stop and target in points, or an `Evaluation`
   explaining the refusal;
4. be added to the regime eligibility map only if there is an argument for it that
   does not depend on a backtest;
5. be measured by SKEPTIC before it can ever be enabled for live execution.
