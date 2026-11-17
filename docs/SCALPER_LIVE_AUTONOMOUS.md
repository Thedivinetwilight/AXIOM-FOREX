# AX-SCALPER LIVE workstation lane

The continuous REAL-only AX-SCALPER runner is wired into `/live`. It uses the
existing guarded MT5 adapter and order route. The controls do not connect,
switch accounts, or send on page load. Nothing here claims profitability or
that a strategy has been admitted.

**Not yet ready for autonomous real trading:** some unresolved requests cannot
be reconciled through the workstation after restart because a refused recovery
runtime is discarded. The autonomous page also lacks a complete durable
outcome history and broker-sourced P&L view. Its position table is not independent
proof of current broker protection. Preserve all state, verify positions in MT5,
and do not resend or bypass the blocker. See `SCALPER_LIVE_WORKSTATION.md` for
the current limitations and safe update steps.

## Guarded path

```text
explicit Connect on Setup
  -> already-connected MT5 observation session
  -> verified REAL account and exact local account binding
  -> signed admission policy and independent raw replay
  -> operator Arm and Start on /live
  -> fresh quote validation and one admitted strategy
  -> Guardian limits and broker-grid sizing
  -> Trader -> BrokerVenue -> LiveLeg -> ExecutionRouter -> MT5TradingAdapter
  -> broker read-back for entry protection
  -> persisted position management, exits and exact-id reconciliation
```

`BrokerVenue` implements the scalper venue protocol but holds no adapter. There
is no direct order-send path in the workstation UI. `LiveLeg` and the adapter
re-check REAL account identity, LIVE mode, arming, evidence lifecycle, symbol
terms, unresolved work, volume grid, margin, quote age and protection before a
submission. PAPER has no adapter and its local capital is never used by this
LIVE lane.

## What Arm and Start require

Arm is refused unless all these checks pass at the time of the action:

* MT5 is explicitly connected and positively reports `REAL`.
* The local server, login fingerprint and currency exactly match MT5.
* Execution mode is confirmed `LIVE`, and the shared adapter is available and
  not held by another LIVE workflow.
* The local LIVE settings parse against the terminal's exact, tradable Forex
  symbol and broker-reported instrument terms.
* Costs, margin and every required risk limit are explicitly declared.
* A strategy's signed evidence passes the production independent raw-data
  replay verifier and the active policy.
* No unrelated broker position, unresolved order, pending protection change or
  unsafe stored run state exists.
* The broker reports a valid protective stop for the exact symbol.

Arm requires the account fingerprint and `ARM AX-SCALPER LIVE`. It sends no
order. Start separately requires the same candidate and fingerprint plus
`START AX-SCALPER LIVE`; it rechecks account, evidence, risk, recovery and mode.
The host reads current balance and equity from the adapter; it does not derive
capital from PAPER or copy account values into configuration.

## Stop, unknown outcomes and restart

Stop disables new entries but does not close broker positions. While the
workstation is running, the owner thread continues to manage the exact open
positions using fresh quotes and verified broker protections. If an entry or
close result is unknown, the request stays latched and can only be reconciled by
its original durable request id; it is never re-sent.

Run state is written atomically and flushed to disk before a broker mutation.
Recovery must match the exact account, strategy evidence, instrument, settings,
guardian history, request ids and broker positions/protection. Missing or
mismatched fields leave the book blocked. Shutdown stops entries, disarms and
preserves run state; it never flattens. Once the process exits, only broker-side
protection remains active until exact recovery resumes local management.

The workstation also leaves a reconciliation-only control for exact unresolved
requests from the previous one-shot bridge. New one-shot and DEMO entry forms
are disabled.

## What is still required before it can place a trade

No genuine candidate is established as admitted in this checkout. Test
fixtures, sample data and PAPER results are not strategy evidence. The current
terminal is reported as DEMO, which this lane rejects. A real Windows/MT5
machine must also verify the launcher, account identity, broker symbol and
volume terms, protective stop read-back, entry/close outcomes, restart recovery
and operator stop behavior. Until those items pass, the build can show the
workflow and its blockers, but a live start must remain refused.

For the exact local LIVE JSON template, accepted field values, units and safe
Windows setup, see `SCALPER_LIVE_WORKSTATION.md`.
