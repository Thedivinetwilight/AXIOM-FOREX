# Live execution — the operator's path to a real order

This is the workflow that can send a real order through MetaTrader 5. It exists to
be used deliberately, one decision at a time, and it is the only path in the
running system that can reach the broker.

```
Provider setup  ──►  Trading mode LIVE  ──►  Live execution: choose terms
(broker terms)       (confirmation)           review → arm → Start
                                                   │
                                                   ▼
                                       strategy signal → checks → order
```

Nothing in that chain happens by itself. Connecting arms nothing, enabling LIVE
arms nothing, arming sends nothing, and starting the session sends nothing until
the strategy actually signals.

## What the operator chooses, every time

| Decision | Who | Where it is recorded |
| --- | --- | --- |
| Strategy (candidate) | operator | `live_plan.local.json`, `LIVE_PLAN_ARMED` audit event |
| Symbol | operator | same |
| Order size (lots) | operator | same, checked against the broker's own volume grid |
| Maximum open positions | operator | same; may **tighten** the provider setup, never widen it |
| Daily loss limit (account currency) | operator | same; same tightening rule |

There is no default for any of them. A blank field is a named problem on the
page, not a substituted value. AXIOM will not pick a strategy, a symbol, a size,
or a limit on your behalf, and it will not claim a strategy is validated when it
is not.

**Unvalidated strategies are labelled, not blocked.** If the candidate has no
measured, passed evaluation behind it, the plan, the audit event, and every screen
say `UNVALIDATED`, and the review states plainly that selecting it is a decision
taken without evidence. The *paper* runner keeps the stricter rule (it will not
run an unselected candidate at all); the live path lets an operator who has read
that warning run one. That is a deliberate difference, and it is the only one.

## The four verbs, and what each may touch

| Action | Writes | Sends | Can refuse? |
| --- | --- | --- | --- |
| **Enable LIVE** (Trading mode) | mode file + audit | nothing | yes — preflight + typed phrase |
| **Arm** (Live execution) | `live_plan.local.json` + audit | nothing | yes — every check below |
| **Start** (Live execution) | runner journal | nothing on its own | yes — unarmed/disconnected/unconfirmed |
| **Stop and disarm** | audit | nothing | never — always available |

Switching to paper, pressing Disconnect, a failed check, an unknown order outcome,
or the workstation shutting down all stop the loop and disarm. There is no state
in which the loop keeps running unarmed.

## The order path, per signal

After your strategy signals, the same objects paper uses decide, and then:

1. **Duplicate check** — a request id that already produced a broker order is a
   duplicate, never a second order.
2. **Blocker sweep** — LIVE and confirmed, router present, adapter connected and
   armed, armed account identical to the account the terminal has selected *now*,
   trading enabled in the settings document, no unresolved order, ledger agrees
   with the terminal's open positions, symbol terms still match the broker's.
3. **The pre-trade gate** — quote freshness, market hours from *your* calendar,
   spread, margin, exposure, daily loss, trade count, entry cap. Unchanged from
   paper; no live-specific relaxation exists.
4. **Route** — the gate's approved intent goes to the MT5 adapter, which
   re-checks price drift, the volume grid, netting conflict, margin and quote age.
5. **Durable intent first** — the order record is written *before* `order_send`.
   If the outcome is unknown, the adapter disarms and the order stays in doubt.
6. **Never re-send** — an in-doubt order blocks every later submission until it is
   reconciled from broker history. Nothing retries automatically.
7. **Book the broker's answer** — a confirmed fill is booked into the same ledger
   paper uses, at the terminal's own price and the filled volume. A fill that
   cannot be booked stops the loop instead of being dropped.

## What the page tells you

The Live execution page always answers one question in a single line:

* *"The next strategy signal WILL send a real order to the broker."* — armed,
  connected, LIVE confirmed, session running, no blockers.
* *"Armed, but the live session is not running…"* — nothing is being read.
* *"No real order can be sent right now."* — paper, disarmed, or disconnected.

Alongside it: the plan on disk, every blocking reason with a hint, the review you
armed, the account the orders would go to (login masked, server, mode, account
signature), the broker's open positions, and the last order attempt with its
retcode and whether the fill was booked.

No password, token, balance, or equity figure is ever shown, logged, or stored.

## Before your first real order

All of these must be true, and the page names each one that is not:

1. `mt5_settings.local.json` exists, with `"mode": "TRADING_ENABLED"`.
2. MetaTrader 5 is installed, open, and signed in to the account you mean to use.
3. `provider_setup.local.json` exists and loads — **Provider setup** writes it.
4. You pressed **Connect**; the terminal reports the expected account and
   `trade_allowed`.
5. **Trading mode → LIVE**, with its preflight clean and the phrase typed.
6. On **Live execution**: strategy, symbol, size, position count and daily loss
   entered, the review acknowledged, `ENABLE LIVE TRADING` typed, **Arm**.
7. **Start** — only then does a signal reach the broker.

The daily loss limit and the open-position count are enforced by AXIOM before each
order. AXIOM does **not** place a stop-loss or take-profit at the broker, so
closing the terminal does not close your risk — that has to be part of your own
plan.

## Files

| File | Contains | Git |
| --- | --- | --- |
| `provider_setup.local.json` | broker terms, costs, calendar, risk limits | ignored (`*.local.json`) |
| `live_plan.local.json` | the armed plan: strategy, symbol, size, limits | ignored |
| `paper_runner.local.sqlite3` | durable orders, intents, fills, audit | ignored |
| `mt5_settings.local.json` | terminal path, server, login, mode | ignored, never read or written by the live flow |

No credential is written to any of them. The MT5 password belongs to the terminal
and to the OS vault, and AXIOM never asks for it in the browser.

## What this does not claim

Wiring a live path is not evidence that any strategy makes money. Measurements in
this project are historical, labelled by data class, and can be wrong; the Live
execution page states the evidence state rather than a forecast. Nothing here is
a profitability claim, and no part of the interface describes the strategy as
"ready" or "profitable".
