# AX-SCALPER — risk controls

Three layers decide whether a trade may exist, and they are deliberately separate.
Each has one job, and none of them can do another's job:

| Layer | Question | Owner |
| --- | --- | --- |
| EV CHECK | Is this expected to make money **after** costs, given an uncertain probability? | `ev.py` |
| VARIANCE | What is the largest size whose stop-loss fits inside the permitted loss? | `variance.py` |
| AX-GUARDIAN | May this account take **another** position at all, right now? | `guardian.py` |

A refusal at any layer is final for that signal. There is no "override", no
"confirm anyway", and no code path that turns a refusal into an order.

## 1. EV CHECK: cost is part of the decision

```
gross   = p × target − (1 − p) × stop            (points)
cost    = spread + 2 × slippage + commission     (round turn, in points)
net     = gross − cost
lower   = net − z × (σ_p × (target + stop))      (z from config, default 1.0)
accept  ⟺ cost is complete ∧ net > 0 ∧ lower ≥ min_edge_points
```

* **A cost that is not known is not zero.** If the spread has not been observed, or
  the commission is `UNKNOWN`, `cost_for` returns `complete=False`, the verdict is
  `REJECT`, and the missing terms are named in the ledger.
* **The decision is taken on the lower bound**, so a point estimate that barely
  clears the floor is refused. A wide posterior therefore has to earn a wider edge
  before it may trade — that is the "uncertainty-aware" part, and it is why a
  freshly started desk with a Jeffreys prior does nothing.
* Spread is measured from the quote at decision time, not from a table. Slippage is
  charged **per side**. Swap is declared `UNKNOWN` and is out of scope for a scalp
  held for minutes; it is never silently zero.

`ExpectancyTracker` records realised net points per fingerprint, so a strategy whose
measured expectancy drifts away from the EV model is visible in the ledger rather
than being discovered by the account.

## 2. VARIANCE: size from the stop, never from capital

```
permitted loss = min(equity × risk_fraction, max_loss_per_trade_account) × regime multiplier
lots           = permitted loss ÷ (stop_points × point_value_per_lot)
```

then floored to the broker's volume step, and capped by aggregate open risk,
leverage, currency concentration and the open-position count. The smallest cap wins.

Rules that are not negotiable:

* **The stop defines the risk.** A signal without a positive stop is refused with
  `stop_distance_not_positive`; there is no default stop and no ATR guess here.
* **Capital is not the maximum loss.** "Risk 1 % of equity" is implemented as *1 % at
  the stop*, not as a notional or margin fraction. Margin and notional are reported
  separately, for the leverage and concentration checks.
* **No arbitrary percentages.** `risk_fraction` is a declared number in configuration,
  bounded to (0, 2 %]; it is not chosen per trade and not adjusted after a loss.
* **Rounding never increases risk.** A size that would round below the broker minimum
  is refused (`size_below_the_broker_minimum`) rather than rounded up.
* **Volatility scaling only reduces.** `volatility_scale` is capped at 1.0, with a
  floor of 0.25. Scaling *up* in quiet markets is how a variance sizer silently
  becomes a leverage sizer.
* **Kelly is available and capped.** `fractional_kelly()` exists for analysis, takes a
  fraction (default ¼) and clamps to a cap (default 1 %). It is not wired into the
  sizing path: an edge validated on synthetic data cannot justify a Kelly fraction.

## 3. AX-GUARDIAN: one account-level authority

`Guardian` is constructed from `GuardianLimits`, which is frozen. One instance can be
shared by the bar engine and the scalper — that is the point: two engines that each
believe they own the risk budget will together exceed it.

Checks, all of which run on every proposal (none short-circuits, so the ledger shows
every reason):

| Check | Refusal |
| --- | --- |
| entries permitted (not suspended, not emergency-stopped, no daily-loss or drawdown breach, no loss-streak breach) | `entries_suspended`, `emergency_stop_active`, `daily_loss_limit_reached`, `drawdown_limit_reached`, `loss_streak_limit_reached` |
| equity known and positive | `equity_not_known` |
| per-trade risk ≤ min(absolute cap, equity × fraction) | `per_trade_risk_exceeded` |
| open risk + new risk ≤ equity × aggregate fraction | `aggregate_open_risk_exceeded` |
| open positions < cap | `max_open_positions_reached` |
| open notional + new ≤ equity × max leverage | `leverage_limit_exceeded` |
| currency exposure ≤ equity × max leverage × concentration fraction | `currency_concentration_exceeded` |
| correlated open positions < cap (fails closed with no matrix) | `correlated_exposure_exceeded`, `correlation_unknown` |
| spread observed, under soft and hard ceilings | `spread_not_observed`, `spread_above_limit` |
| quote age ≤ limit | `quote_stale` |
| decision latency ≤ budget | `decision_latency_exceeded` |
| fill latency ≤ limit (from the first observed fill) | `fill_latency_exceeded` |
| slippage ≤ limit (observed, else the declared estimate, else refuse) | `slippage_limit_exceeded` |

Two deliberate design notes:

* **Currency concentration is a share of the leverage allowance**, not of equity.
  A single 0.13-lot EUR/USD position is ~1.4× equity in notional; an equity-sized cap
  would refuse the first ordinary lot, which makes a limit meaningless rather than
  protective.
* **The fill-latency check is not a loophole.** Before any fill exists there is
  nothing observed to compare, and the ledger says exactly that. From the first fill
  onward the check runs on measured numbers. Slippage has the same shape, but falls
  back to the *declared* estimate rather than to zero.

### Entry suspension is not position management

```
may_open()   → False when suspended, emergency-stopped, or any account limit is breached
may_manage() → always True
```

The desk keeps running the management loop while entries are suspended: stops are
verified, break-even and trailing moves are applied, time exits and stale-quote exits
fire. A guardian that could lock itself out of the exit path would not be a
guardian.

### Emergency stop: the defined position-protection procedure

`Guardian.emergency_stop(reason, at_ms)` returns a `ShutdownPlan` and changes nothing
else. The procedure it defines is:

1. **Stop new entries.** `may_open()` becomes `False` for the account, immediately.
2. **Do not abandon a position.** No position is closed, modified or forgotten by the
   stop itself.
3. **Protection stays verified.** Every open position's broker-side stop is listed in
   `protected_positions`; anything without one appears in `unprotected_positions` and
   is reported as a blocker. The management loop continues.
4. **Flattening is a separate, deliberate operator act** — `desk.flatten_all()` — and
   it records every close. Positions with no current quote are **not** skipped
   silently: they stay open and visible, with `no_quote_available` written down.
5. **Clearing the stop requires an operator name** (`clear_emergency(operator=...)`),
   and the clearing is written to the ledger with that name.
6. **Nothing resumes automatically.** A resolved-but-unreviewed emergency leaves
   entries suspended until an operator resumes them.

## 4. Limits are not agent-configurable

No strategy, no probability model and no desk component can change a limit. The
limits object is frozen, and `Guardian.request_limit_change(requested_by=...)` always
answers:

```json
{"accepted": false, "reason": "limits_are_not_agent_configurable", "logged": true}
```

The attempt is written to the ledger. Changing a limit means constructing a new
`GuardianLimits` from a new configuration — an operator action that appears in the
diff, the review and the restart.

## 5. What none of this proves

These controls bound the damage of being wrong. They do not make a strategy right,
and they cannot price a broker term the configuration does not know. The desk says so
in its own status output rather than implying completeness.
