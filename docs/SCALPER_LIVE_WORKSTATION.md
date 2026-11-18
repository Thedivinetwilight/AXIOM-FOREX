# Start AX-SCALPER LIVE on the workstation

This guide describes the continuous MT5 runner on `/live` for a positively identified
DEMO or REAL account. The checks are the same for both. It uses the workstation's already-connected MT5 session
and existing guarded adapter; it does not connect, switch accounts, or send on
page load.

## Current readiness

The runner and its `/live` controls are wired. **The branch does not contain a
candidate proven eligible for live trading.** A real signed evidence package
must pass the independent raw-data replay and current admission policy before
the Arm control can appear. Synthetic test fixtures are not strategy evidence.
The selected account and all broker-specific values below must also match. Until
those conditions are true, the page explains the blocker and Start stays
unavailable. Do not bypass an admission refusal or use this as a profitability
claim.

A previous report described the terminal account as DEMO; its current state has
not been checked during this change. Sign in to the
intended DEMO or REAL account in MetaTrader 5 yourself and confirm the server and
account before using the runner. AXIOM does not switch accounts.

**Integration remains incomplete.** In particular, when restart recovery of an
unresolved autonomous request returns a refusal, the workstation host discards
the recovery runtime, so the `/live` reconciliation control cannot always
reach that request after restart. Preserve the state and broker records; do not
resend, delete or rewrite them to get past the blocker. Empty broker history
is not proof that an order was never placed: without affirmative evidence the
request stays unresolved. A cancelled order with reported deals also stays
blocked for exact execution reconciliation. The broker monitor
shows account-bound broker positions and locally recorded orders when available;
its open-position profit is not a complete accounting of closed P&L or costs.
An unreadable position makes the position list and profit unavailable rather
than silently omitting it. Broker read-back is not independent verification of
the integration: verify protection in MT5. Do not use this build for autonomous
real trading until the recovery gap and the machine-side checks below have been
resolved and independently verified.

Run-state snapshots now bind account type and currency in addition to server
and fingerprint. Older v2 snapshots lack those fields and are deliberately
refused; preserve the original file and broker records for exact reconciliation.
Do not delete or rewrite a snapshot to bypass this check. While a run is in
`RECOVERY_REQUIRED`, Stop and Shutdown keep its existing snapshot unchanged and
send no flatten order; local management is paused, so verify broker protection
independently. An inconclusive exact-request query also leaves the snapshot
unchanged. Broker order recovery also requires the original account mode,
server, fingerprint, login and currency and a matching broker order ticket;
older records lacking those facts remain blocked and preserved. Connecting does
not resolve an autonomous request without validating its run context. These
guards do not make restart reconciliation complete.

## Local LIVE configuration

The ignored file is `scalper_live_settings.local.json` in the repository root.
Create it only after connecting to the intended MT5 account. The template below
is intentionally incomplete: replace every blank with a broker-verified or
operator-approved value. Empty values fail validation. Do not copy PAPER
capital or guess a cost, contract size, margin term, or risk limit.

```json
{
  "schema": "axiom-scalper-live-v1",
  "symbol": "",
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
  },
  "account": {
    "server": "",
    "login": "",
    "currency": "",
    "fingerprint": ""
  }
}
```

`symbol` must exactly match the suffix-bearing, currently tradable Forex symbol
reported by the connected terminal. Instrument contract, point, digits and
volume grid are read from MT5; do not add them to this file. `margin_rate` is a
fraction taken from the broker's applicable terms. `account.login` is the
numeric login shown by MT5. `account.fingerprint` must equal AXIOM's one-way
fingerprint for that login; it is an identity binding, not a password. Keep the
file private and local.

| Setting | Unit / accepted meaning |
| --- | --- |
| `commission_per_lot` | Account currency per lot for a round turn; must be non-negative. |
| `commission_state` | `OBSERVED` or `ESTIMATE`. |
| `slippage_points` | Points per side; must be non-negative. |
| `slippage_state` | `OBSERVED` or `ESTIMATE`. |
| `swap_state` | `OBSERVED` or `ESTIMATE`; unknown financing blocks use. |
| `risk_fraction`, `max_aggregate_risk_fraction`, `max_daily_loss_fraction`, `max_drawdown_fraction`, `max_currency_exposure_fraction` | Declared fractions, not percentages. `max_currency_exposure_fraction` must be in `(0, 1]`. |
| `max_loss_per_trade_account` | Positive amount in the account currency. |
| `max_leverage` | Positive leverage ratio. |
| `max_correlated_positions`, `max_loss_streak`, `max_fill_latency_ms`, `max_open_positions`, `max_quote_age_ms`, `max_hold_ms` | Positive whole numbers; time fields are milliseconds. |
| `max_spread_points`, `max_spread_points_hard`, `max_slippage_points`, `break_even_trigger_points`, `trail_after_points`, `trail_distance_points` | Broker points; spreads and slippage must be non-negative, and the hard spread cap must be no lower than the soft cap. |

All risk and cost fields are required. Additional tick-quality and decision
parameters use only the validated defaults defined by the current code. The
runner reads starting balance and equity from the confirmed MT5 terminal at
explicit Start; no balance is entered or copied into this file.

## Evidence and explicit controls

1. Launch AXIOM using the project's virtual environment and the same MT5
   settings used for the already-connected terminal. Keep the workstation bound
   to loopback.
2. On **Setup**, press **Connect** yourself. Confirm the page reports the
   intended server, DEMO or REAL account type, account currency, and fingerprint.
3. Put the independently reviewed, signed evidence and admission policy in the
   local paths documented by `SCALPER_ADMISSION.md` and
   `SCALPER_RAW_REPLAY_V2.md`. They must bind the exact strategy, symbol,
   broker instrument, costs and configuration. A PAPER result cannot qualify a
   candidate.
4. Fill the LIVE settings file with actual verified terms. Reload `/live` and
   resolve every displayed blocker. Do not arm while any blocker remains.
5. **Arm AX-SCALPER** requires the exact candidate, account type plus fingerprint and
   phrase `ARM AX-SCALPER LIVE`. Arm changes no broker position and sends no
   order. **Start AX-SCALPER LIVE** is a separate action requiring the same
   candidate, account type plus fingerprint and `START AX-SCALPER LIVE`.
6. The page reports quote age, decisions/refusals, runner state and managed
   positions, with the monitoring limitations noted above. **Stop new entries**
   does not close broker positions. The local manager attempts to continue only
   while its account, quote and execution guards pass; broker protection must be
   checked independently. Close and protection requests use the guarded MT5
   route. An unknown outcome must be reconciled by its original durable request
   id and never resubmitted; the restart limitation above can block this.

Do not close the workstation while this process is the only manager of open
positions unless you have verified their broker-side protection and accepted
that local trailing/time management will stop until the exact state is
recovered. Shutdown disarms new submissions, never flattens, and preserves the
durable run-state file.

## Updating and launching on Windows

Stop the workstation during a maintenance window. Preserve and inspect local
settings and PAPER data first. From the intended feature-branch clone:

```powershell
git status --short --branch
git fetch origin
git log --oneline HEAD..origin/arena/5a98c3d9-axiom-forex
git merge --ff-only origin/arena/5a98c3d9-axiom-forex
.\.venv\Scripts\python.exe -m pip install -e ".[evidence]"
.\.venv\Scripts\python.exe -m pip install MetaTrader5
.\.venv\Scripts\python.exe -m axiom_forex.workstation.launch
```

This merges the named remote tip only if the current branch can fast-forward;
it does not switch branches. The existing `scripts/update-workstation.cmd` is
pinned to a different historical branch and should not be used for this update.
If Git reports local changes or the fast-forward is not possible, stop and
preserve them; do not reset, clean, or copy over an existing settings or state
file. The package-install commands do not connect to MT5. After launch, use the
browser page at the loopback address printed by the workstation. Connect is a
separate operator action.

This repository's fake-terminal tests do not validate a Windows MT5 build,
broker execution, a genuine admitted strategy, or profitability. Do not send a
REAL order until the required independent evidence has been reviewed and the
REAL-account, cost, risk, stop, restart and unknown-outcome paths have been
verified on the intended machine.
