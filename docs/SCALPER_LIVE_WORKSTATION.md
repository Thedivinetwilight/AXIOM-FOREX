# Start AX-SCALPER LIVE on the workstation

This guide describes the continuous REAL-only runner on `/live`. It never
accepts a DEMO account. It uses the workstation's already-connected MT5 session
and existing guarded adapter; it does not connect, switch accounts, or send on
page load.

## Current readiness

The runner and its `/live` controls are wired. **The branch does not contain a
candidate proven eligible for live trading.** A real signed evidence package
must pass the independent raw-data replay and current admission policy before
the Arm control can appear. Synthetic test fixtures are not strategy evidence.
The REAL account and all broker-specific values below must also match. Until
those conditions are true, the page explains the blocker and Start stays
unavailable. Do not bypass an admission refusal or use this as a profitability
claim.

The currently reported terminal account is DEMO. This lane will refuse it. Sign
in to the intended REAL account in MetaTrader 5 yourself and confirm the server
and account before using the runner. AXIOM does not switch accounts.

## Local LIVE configuration

The ignored file is `scalper_live_settings.local.json` in the repository root.
Create it only after connecting to the intended REAL account. The template below
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
runner reads starting balance and equity from the confirmed REAL terminal at
explicit Start; no balance is entered or copied into this file.

## Evidence and explicit controls

1. Launch AXIOM using the project's virtual environment and the same MT5
   settings used for the already-connected terminal. Keep the workstation bound
   to loopback.
2. On **Setup**, press **Connect** yourself. Confirm the page reports the
   intended server, REAL account type, account currency, and fingerprint.
3. Put the independently reviewed, signed evidence and admission policy in the
   local paths documented by `SCALPER_ADMISSION.md` and
   `SCALPER_RAW_REPLAY_V2.md`. They must bind the exact strategy, symbol,
   broker instrument, costs and configuration. A PAPER result cannot qualify a
   candidate.
4. Fill the LIVE settings file with actual verified terms. Reload `/live` and
   resolve every displayed blocker. Do not arm while any blocker remains.
5. **Arm AX-SCALPER** requires the exact candidate, account fingerprint and
   phrase `ARM AX-SCALPER LIVE`. Arm changes no broker position and sends no
   order. **Start AX-SCALPER LIVE** is a separate action requiring the same
   candidate, fingerprint and `START AX-SCALPER LIVE`.
6. The page monitors quote age, decisions/refusals, runner state, durable
   requests, managed positions and available P&L. **Stop new entries** leaves
   open positions in the broker and keeps their existing protective orders.
   Close and protection requests use the same guarded MT5 route. An unknown
   order result is reconciled by its original durable request id and is never
   resubmitted.

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
git log --oneline HEAD..origin/arena/ddec41f3-axiom-forex
git merge --ff-only origin/arena/ddec41f3-axiom-forex
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install MetaTrader5
.\.venv\Scripts\python.exe -m axiom_forex.workstation.launch
```

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
