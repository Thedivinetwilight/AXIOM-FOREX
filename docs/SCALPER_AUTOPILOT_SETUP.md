# AX-SCALPER PAPER Autopilot setup

## Safe Windows fast-forward update

Do not update files or the Python environment while the previous patch is actively
trading. This task did not stop, restart, connect to, or alter that running system.
Use the steps below only in a maintenance window after the operator has independently
made the existing system safe to update. Preserve local settings and paper-state
folders; these steps never reset or clean them.

In PowerShell, change to your existing AXIOM checkout and inspect it first:

```powershell
Set-Location C:\path\to\AXIOM-FOREX
git status --short --branch
git fetch origin
git switch arena/f67c2859-axiom-forex
git merge-base --is-ancestor HEAD origin/arena/f67c2859-axiom-forex
if ($LASTEXITCODE -ne 0) { throw "Local branch is not behind the published feature branch; stop and review before updating." }
git merge --ff-only origin/arena/f67c2859-axiom-forex
```

If `git status` lists any modified, staged, untracked, ignored, or local paper-state
files that matter to you, stop before updating and preserve/review them separately;
do not run `reset`, `clean`, overwrite local settings, or use a forced update. If the
branch does not exist locally, inspect the checkout and remotes first rather than
creating a similarly named branch automatically. The merge command above refuses
anything except a fast-forward.

From the repository root, use the existing virtual environment. If it does not
exist, create it; do not recreate or replace an existing environment:

```powershell
if (-not (Test-Path .\.venv\Scripts\python.exe)) { py -3.11 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip install MetaTrader5
.\.venv\Scripts\python.exe -m axiom_forex.workstation.launch
```

The project declares `tzdata` as a Windows-only runtime dependency, so the editable
install includes it. If Windows `zoneinfo` still reports a missing database, install
it explicitly with `.\.venv\Scripts\python.exe -m pip install tzdata` in that same
environment. The `MetaTrader5` package is needed only for MT5 integration; install
it without connecting or logging in. Do not execute the update or these install
commands while the previous patch is actively trading. The exact Windows install,
package compatibility, Connect and launch sequence have not been verified in this
change; see the Windows checks below.

AX-SCALPER appears on `/autopilot` as a distinct **PAPER** panel. It reads live
quotes only from the workstation's observation session after the operator presses
**Connect** on Setup. Starting or stopping AX-SCALPER does not connect or disconnect
the terminal. Stop leaves local paper positions open; it does not flatten them or
touch broker positions. The symbol picker only offers exact terminal-verified,
tradable Forex names and its explicit Save action changes only the `symbol` field
in the local scalper settings; every other existing term is preserved. The host
currently has **zero admitted strategies**: Start can monitor/record quotes, but
returns a no-trade admission for every candidate until an independent broker-symbol
out-of-sample evidence artifact passes the displayed, predeclared SKEPTIC policy.
Paper outcomes from that same live run are not training evidence and cannot promote
a strategy.

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

## Operator flow (current no-trade release)

1. After a separately planned safe maintenance update, launch the workstation and
   press **Connect** on Setup yourself. AXIOM does not connect/reconnect or switch
   the terminal account in the background.
2. On `/autopilot`, review the verified symbol list and exclusions. Choose one
   exact broker name in the picker and press **Save PAPER symbol selection**. This
   modifies only `symbol` in the local scalper settings.
3. Declare each missing cost, margin and risk term only from an operator-verified
   source. Set local simulated capital on the existing Performance page. Never use
   MT5 balance as paper capital; do not invent costs, margin, limits or risk values.
4. Review the strategy policy/evidence card. As shipped, no candidate is admitted
   because independent broker-symbol OOS evidence is absent and drawdown/tail-loss
   and portfolio-correlation cutoffs remain undecided. **No PAPER orders/fills or
   LIVE intents are produced.** Start currently means quote monitoring and refusal
   auditing only. Stop ends that monitoring and leaves any local positions open.
5. Check status, accepted ticks and freshness. If a recovery blocker appears, keep
   the ledger and sidecar unchanged; the acknowledgement records review only and
   does not unlock Start.

LIVE is separate and is not started from the AX-SCALPER PAPER controls. The existing
`/live` workflow is the sole broker-order path; its exact operator steps and guards
are documented in `docs/LIVE_EXECUTION.md`. AX-SCALPER has no eligible LIVE intent
or direct adapter. Do not treat its quote monitor or a synthetic/replay result as
LIVE authorization.

## Recovery boundary

Each run has its own append-only, hash-verified JSONL ledger under
`scalper_paper_ledgers` beside the local settings. A failed integrity check or
unclosed local paper position from an earlier run blocks a new run. The current
AX-SCALPER desk does not yet reconstruct the full strategy/guardian/position
manager state exactly; therefore this host does not auto-resume or auto-flatten an
open position. It shows stored identity, fill, quote and risk fields when present,
and missing fields; the run remains blocked after the review acknowledgement.
Preserve the JSONL ledger and `.head.json` checkpoint unchanged, reconcile read-only,
and do not edit/delete them or attempt another run. There is no safe in-app
operation to clear this blocker yet. Exact recovery tooling is an open
implementation item; acknowledgement is not recovery or resume. This is a
fail-closed limitation, not a license to ignore a position.

## Important admission limitation

A configured desk without a calibrated, approved outcome artifact refuses entries
for insufficient probability evidence. This page does not make a strategy eligible
or claim that a live quote session establishes profitability. No profit guarantee
or live-readiness claim is made. LIVE controls remain only on `/live`.
