# AX-SCALPER PAPER Autopilot setup

AX-SCALPER appears on `/autopilot` as a distinct **PAPER** panel. It reads live
quotes only from the workstation's observation session after the operator presses
**Connect** on Setup. Starting or stopping AX-SCALPER does not connect or disconnect
the terminal. Stop leaves local paper positions open; it does not flatten them or
touch broker positions.

## Local declaration file

The launcher looks for `scalper_paper.local.json` next to the workstation's local
MT5 settings file. It is an operator-maintained local file, not committed to Git.
Use this shape, replacing each empty value only with a value you have verified or
declared. Empty values deliberately fail validation; no example risk or cost
number is offered as a recommendation.

```json
{
  "schema": "axiom-scalper-paper-v1",
  "symbol": "<exact terminal broker symbol>",
  "margin_rate": "",
  "costs": {
    "commission_per_lot": "",
    "commission_state": "",
    "slippage_points": "",
    "slippage_state": "",
    "swap_state": ""
  },
  "risk": {
    "risk_fraction": "",
    "max_loss_per_trade_account": "",
    "max_aggregate_risk_fraction": "",
    "max_leverage": "",
    "max_currency_exposure_fraction": "",
    "max_correlated_positions": "",
    "max_daily_loss_fraction": "",
    "max_drawdown_fraction": "",
    "max_loss_streak": "",
    "max_spread_points_hard": "",
    "max_slippage_points": "",
    "max_fill_latency_ms": "",
    "max_open_positions": "",
    "max_quote_age_ms": "",
    "max_spread_points": "",
    "max_hold_ms": "",
    "break_even_trigger_points": "",
    "trail_after_points": "",
    "trail_distance_points": ""
  }
}
```

`symbol` must byte-match an AXIOM-verified, tradable Forex symbol from the connected
terminal. Point, digits, contract size and volume bounds come from that terminal's
read-only symbol specification. Quote currency must equal the local paper capital
currency for now; otherwise a current verified conversion feed is required and
Start refuses. The local paper amount/currency is read from the existing
Performance-page capital setting, never from terminal balance/equity. `margin_rate`
is an explicit declared input; it is not guessed from account leverage.

Cost amount units are account currency per round-turn lot for commission and
points per side for slippage. Cost-state values must make uncertainty explicit
(`OBSERVED` or `ESTIMATE`); `UNKNOWN` blocks. `swap_state` must be known because
holding costs are not presumed zero. Risk fields are enforced by the desk and must
satisfy its numeric validation ranges. Fraction fields are decimal fractions
(`0.01` means one percent), amount fields are account currency, point fields are
broker points, and time fields are milliseconds. Invalid, missing, unknown or
extra keys block Start with a reason and remediation.

## Recovery boundary

Each run has its own append-only, hash-verified JSONL ledger under
`scalper_paper_ledgers` beside the local settings. A failed integrity check or
unclosed local paper position from an earlier run blocks a new run. The current
AX-SCALPER desk does not yet reconstruct the full strategy/guardian/position
manager state exactly; therefore this host does not auto-resume or auto-flatten an
open position. Review the named ledger and retain the recovery block until exact
manual recovery is available. This is a fail-closed limitation, not a license to
ignore a position.

## Important admission limitation

A configured desk without a calibrated, approved outcome artifact refuses entries
for insufficient probability evidence. This page does not make a strategy eligible
or claim that a live quote session establishes profitability. No profit guarantee
or live-readiness claim is made. LIVE controls remain only on `/live`.
