# Live execution — the operator's path to a real order

This is the workflow that can send a real order through MetaTrader 5. It exists to
be used deliberately, one decision at a time, and it is the only path in the
running system that can reach the broker.

```
Trading mode  ──►  Provider setup  ──►  Live execution: choose terms  ──►  strategy
PAPER / LIVE       (broker terms)       review → arm → Start               signal
                                                                             │
                                                                             ▼
                                                                  checks → order
```

**Both modes are always offered.** Nothing about the account, the terminal, the
settings, a strategy, or a test run earlier hides or disables either one: you can
switch at any time, in either direction. A mode change sends no order, connects
nothing, arms nothing, and closes nothing.

Selecting LIVE warns that real-money orders may follow from the active strategy,
then asks for two things: the confirmation phrase, and the signature of the
account connected **right now**. That signature is what "LIVE" means for the
session — if the terminal is later signed in to a different account, the orders
are refused rather than sent to it.

Nothing in that chain happens by itself. Connecting arms nothing, enabling LIVE
arms nothing, arming sends nothing, and starting the session sends nothing until
the strategy actually signals.

## What the safeguards are attached to

The checks live on the **order path**, not on the mode switch:

| Check | Where it acts | What it does |
| --- | --- | --- |
| Account identity | every order | refuses if the terminal's account is not the one the session confirmed |
| Broker connection | every order | refuses if there is no session, or the terminal is not trading-enabled |
| Order validity | every order | refuses a stale/crossed quote, bad volume, drift, or missing specs |
| Configured risk limits | every order | refuses size, open-position count, or daily loss beyond the plan |
| Unresolved orders | every order | refuses anything new while an earlier order is in doubt |
| Plan protective stop | every order | an entry is refused while any of our positions is past the plan's limit, or the distance cannot be measured (see below) |
| Broker-side stop loss | every entry | the stop price must be sent, accepted, and echoed back by the broker; an unconfirmed stop disarms and blocks the next entry |

Each of these shows on the page with the reason and the step that clears it. None
of them hides, disables, or reverts the mode you chose. Paper mode itself never
sends a real order: in PAPER the loop is a simulation and there is no order
surface in reach.

Switching to PAPER takes effect immediately: the live session is stopped and
disarmed, so no new live entry can follow. **It does not close anything.** Broker
positions stay open, are listed on the Trading mode and Live execution pages, and
are yours to manage in the terminal — AXIOM never closes a broker position because
the mode changed.

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
| **Select LIVE** (Trading mode) | mode file + audit | nothing | yes — typed phrase + the connected account's signature |
| **Arm** (Live execution) | `live_plan.local.json` + audit | nothing | yes — every check below, plus the account signature typed exactly |
| **Start** (Live execution) | runner journal | nothing on its own | yes — unarmed/disconnected/unconfirmed |
| **Stop and disarm** | audit | nothing | never — always available |
| **Save broker-side stop** (Broker-side stop settings) | `mt5_settings.local.json` | nothing | yes — while armed or running, without a unit, an unreadable value, or a distance inside the broker minimum |

Switching to paper, pressing Disconnect, a failed check, an unknown order outcome,
or the workstation shutting down all stop the loop and disarm. There is no state
in which the loop keeps running unarmed. A switch to paper ends the authorisation,
not the positions: it stops new entries at once and **never** closes a broker
position on your behalf — anything already open is still yours to manage in the
terminal.

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

### The start button on `/autopilot`

`/autopilot` can also start **this** session, once a strategy is selected and running
there. That page's bridge calls the same `start_session()` and displays the same
refusals; it cannot arm, cannot write a plan and cannot change a size or a limit. It
refuses when no plan is written, when the plan belongs to a different candidate than
the running selection, when the mode is not LIVE, when the adapter is not connected,
when the plan is not armed, or when this page reports any blocker. Stopping from
either page disarms and leaves open positions untouched. See `AUTOPILOT.md`.

## Before your first real order

All of these must be true, and the page names each one that is not:

1. `mt5_settings.local.json` exists, with `"mode": "TRADING_ENABLED"`.
2. MetaTrader 5 is installed, open, and signed in to the account you mean to use.
3. `provider_setup.local.json` exists and loads — **Provider setup** writes it.
4. You pressed **Connect**; the terminal reports the expected account and
   `trade_allowed`.
5. **Trading mode → LIVE**: the phrase typed and the connected account's signature
   confirmed. (An outstanding check refuses the order later; it never blocks this
   choice.)
6. On **Live execution**: strategy, symbol, **timeframe**, size, position count,
   **protective stop** and daily loss entered (nothing is pre-filled from paper),
   the review acknowledged, the
   account signature typed exactly as shown, `ENABLE LIVE TRADING` typed, **Arm**.
   Arming is bound to that account: if the terminal is switched to another one
   afterwards, every order is refused rather than routed to the new account.
7. **Start** — only then does a signal reach the broker.

The daily loss limit and the open-position count are enforced by AXIOM before each
order. There is **one** stop-loss layer at the broker and **one** watched limit in
the plan, and they are never described as the same thing:

* the **broker-side stop loss** is attached to every entry from the *per-symbol*
  distance in the local MT5 settings (`protective_stops`, written with its unit).
  It is sent as the order's `sl`, and it only counts once the broker's own position
  record reports it back at that price. If it does not, the position is recorded as
  *unconfirmed*, the adapter disarms, and no further entry is sent;
* the **plan protective stop in pips** is the operator's own limit, watched by
  AXIOM. It blocks new orders when exceeded; it never claims to be a broker stop.

No take-profit is placed. A symbol with no usable distance in `protective_stops`
gets no entry at all: this build refuses to open a position it cannot protect. A broker-side
stop is not a guarantee — gaps, slippage, and a disconnected terminal can all
leave a position beyond it — so it is one part of your plan, not the whole of it.

### The timeframe and the protective stop

The plan states the **bar size** the strategy decides on, and the live loop's own
feed interval is compared with it. A plan written for 15-minute bars is refused by
a stack that reads hourly bars (`live_plan_timeframe_mismatch`), because then the
loop would be deciding on different bars than the ones the evidence came from. The
hint names the exact `--paper-runner-interval` value that would match, or you can
arm a plan for the bar size this stack already reads. The page shows both numbers;
neither is adjusted silently.

The plan also states a **protective stop in pips**, and it is required: a stored
plan without one is refused rather than defaulted. What it does is concrete and
limited:

* it is recorded in `live_plan.local.json` and shown beside the plan and every open
  position;
* an open position that has moved further against the account than that limit —
  measured from the broker's own entry and current prices and the symbol's pip size
  — blocks new orders (`protective_stop_breached`) and says which ticket and by how
  much;
* if the distance cannot be measured (no terminal, no pip size, unreadable
  positions) that is reported as `protective_stop_unmeasurable`, not as a pass.

A plan file written by a build from before these two fields existed therefore
stops working, and that is the intended outcome: the page names the field it
cannot find (`live_plan_timeframe_missing`, `live_plan_stop_missing`) instead of
treating the file as "no plan", and arming again — with the bar size and the stop
answered — is the fix. Nothing is filled in for you, and the old file is left on
disk untouched until the new plan replaces it.

This watched limit is **not** the broker-side stop. It never places, moves, or
cancels anything at the broker: a position beyond it stays open until the
strategy's exit fires, the broker's own stop loss closes it, or you close it
yourself. The page states this in the same words, and the two distances are
listed separately.

### The broker-side stop, and how it is checked

Every entry carries a stop loss one *declared distance for that symbol* away from
the price the order is sent at, in the direction that loses money. The distance is
configured per pair, because one global number is not one risk: 200 points is 20
pips on EUR/USD and 2 pips on USD/JPY.

* the distance is declared **with its unit** — `points` or `pips` — and converted
  once with the symbol's own `point` size and the catalog's verified `pip_size`. A
  distance that does not land on a whole broker point is refused
  (`mt5_protective_stop_not_on_point_grid`) rather than rounded, because rounding
  would move the stop you asked for without saying so;
* the request is refused outright, before the write-ahead record, when the pair has
  no declared distance (`mt5_protective_stop_not_configured_for_symbol`), when the
  value cannot be read (`mt5_protective_stop_invalid`), or when it is inside the
  broker's own `stops_level` (`mt5_protective_stop_below_broker_minimum`). The same
  checks run at **arming** time (`broker_stop_not_configured`,
  `broker_stop_below_broker_minimum`, `broker_stop_invalid`), so a plan that could
  never place a protected order cannot be armed;
* any position this deployment placed that has no stop blocks the next entry
  (`mt5_unprotected_position_present` — the ticket is named);
* after the send, AXIOM re-reads the broker's positions and compares `sl` with
  what it asked for, within one point. `MT5_ORDER_STOP_VERIFIED` means the broker
  reports the stop; anything else is `MT5_ORDER_STOP_UNCONFIRMED` with the reason
  (`broker_reports_no_stop_loss`, `position_not_found_after_send`,
  `stop_loss_differs_by_...`), the adapter disarms, and the order record keeps the
  requested price beside the unconfirmed flag;
* reconciliation reports every position of ours without a stop as
  `unprotected_positions`, and the live page raises
  `live_protective_stop_missing` for it. It is a defect to look at, never a
  position AXIOM silently "fixes";
* a close never carries a new stop, and positions belonging to another expert
  advisor on the same account are not counted as ours.

### Setting the distance

**Live execution → Broker-side stop settings** is the only place a distance is
entered. The card shows the exact broker symbol it applies to, the entered value
*and its unit*, the symbol's `point` and pip size, and the broker's minimum stop
distance in both points and price. Nothing is pre-filled, and the unit is a choice
you have to make: a bare number is refused rather than read as points or pips.

The value is judged against the connected terminal's own symbol record *before* it
is written, and only the `protective_stops` section of `mt5_settings.local.json`
changes — every other key keeps its value. The write is refused while trading is
armed or a session is running, because the adapter reads its settings when it is
built. When the file and the running adapter disagree, the page says so in as many
words (**Saved, not yet in force**) and no live entry is sent until you reconnect.

```json
"protective_stops": { "EUR/USD": "20 pips", "USD/JPY": "15 pips" }
```

The older single `protective_stop_points` value is still accepted in a document
written by an earlier build, but it is **not used for entries**: one global number
is not one risk. The settings view reports it as ignored
(`protective_stop_points_ignored`) rather than quietly obeying it.

The two distances use different units on purpose: the plan's monitored limit is in
**pips**, the broker setting may be in **points or pips** because `stops_level` is
quoted in points. On a 5-digit pair a point is a tenth of a pip, so 20 pips is 200
points. Both are printed with their unit, and the order record keeps the exact
price that was sent.

The broker's acceptance of `sl` on `order_send`, and its echo on `positions_get`,
are exercised against a simulated terminal only. They have **not** been observed on
the operator's live MetaTrader 5 account.

### Stopping

**EMERGENCY STOP** stops sending and disarms, and does nothing else: it closes no
position, changes no mode, and sends nothing. Anything already open stays open in
the terminal; the message after the click says how many positions are still open.
Open positions are never closed because a session stopped, a mode changed, or a
page was reloaded.

## Files

| File | Contains | Git |
| --- | --- | --- |
| `provider_setup.local.json` | broker terms, costs, calendar, risk limits | ignored (`*.local.json`) |
| `live_plan.local.json` | the armed plan: strategy, symbol, size, limits | ignored |
| `paper_runner.local.sqlite3` | durable orders, intents, fills, audit | ignored |
| `mt5_settings.local.json` | terminal path, server, login, mode, per-symbol stop distances | ignored; written by the stop control only, one `protective_stops` entry at a time |

No credential is written to any of them. The MT5 password belongs to the terminal
and to the OS vault, and AXIOM never asks for it in the browser.

## What this does not claim

Wiring a live path is not evidence that any strategy makes money. Measurements in
this project are historical, labelled by data class, and can be wrong; the Live
execution page states the evidence state rather than a forecast. Nothing here is
a profitability claim, and no part of the interface describes the strategy as
"ready" or "profitable".
