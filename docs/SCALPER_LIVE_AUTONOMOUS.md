# AX-SCALPER on a real account: what is built, and what still blocks it

This note records the increment that put AX-SCALPER on a verified **real-money**
account, and — just as importantly — what it does **not** yet do. Nothing here is
a claim of readiness, profitability, or admitted evidence.

## The path, end to end

```
Connect (operator, explicit)
   └─ MT5Terminal / MT5TradingAdapter            account_info() says REAL? server? login?
        └─ scalper.live_account.verify_real_account   ← the gate this increment added
             └─ LiveLeg.blockers()                     ← re-run before every submission
                  └─ AutonomousScalper.on_tick()      ← one decision per validated quote
                       ├─ TickCore.ingest             freshness, spread, gap, sequence
                       ├─ AdmittedStrategy.evaluate   the ONE admitted strategy
                       ├─ Guardian.evaluate           declared limits, equity, exposure
                       ├─ size from risk fraction     clamped to the broker's grid
                       └─ Trader.execute              submit once, then verify protection
                            └─ BrokerVenue.submit     ← implements the scalper's venue protocol
                                 └─ LiveLeg.execute   ← existing: blockers → gate → router
                                      └─ MT5TradingAdapter.submit
                                           write-ahead → order_check → order_send
                   └─ PositionBook.on_tick            stop / target / time exits
                        └─ BrokerVenue.close          ← the same leg, a close intent
                   └─ BrokerVenue.query               ← the durable record, never a re-send
```

There is exactly one order route. `BrokerVenue` imports nothing from
`axiom_forex.brokers.mt5`, holds no adapter, and cannot send what `LiveLeg`
would not send.

## What the real-account check does

`verify_real_account(status, expected)` reads the adapter's own report and asks:

| Question | Refusal reason |
| --- | --- |
| Is the terminal connected? | `mt5_not_connected` |
| Did it confirm the account type at all? | `account_mode_not_confirmed` |
| Is it REAL (not DEMO, not CONTEST, not unclassified)? | `real_account_required` / `account_mode_unknown` |
| Is it the declared server? | `account_server_mismatch` |
| Is it the declared login? (checked through the one-way fingerprint, because the adapter redacts logins) | `account_login_mismatch` |
| Is it the declared fingerprint / currency? | `account_fingerprint_mismatch` / `account_currency_mismatch` |

`LiveLeg(require_real_account=True)` runs that verdict inside `blockers()`, which
every submission runs — so it is checked before **every** order, and a terminal
that changes account mid-run stops the lane at the next attempt. AXIOM never
switches accounts and never reconnects silently.

## What the autonomous run does

* **Arm** is refused without an admitted strategy, a verified REAL account,
  confirmed LIVE mode, an armed adapter and a clean reconciliation.
* **Start** begins entry decisions; **pause** stops them and keeps management;
  **stop** stops entries, and reports the open positions at the broker without
  closing anything unless the operator asks (`flatten`).
* A versioned `JsonRunStateStore` uses atomic replacement plus file and
  directory `fsync`. The run persists admission/config identity, guardian
  observations and exposure, lifecycle counters, the complete PositionBook
  (including partial exits and pending close/protection state), and any entry
  intent. Live readiness refuses a missing or failed durable store.
* Entry intents are fsynced before submission. A restart queries that exact
  request id; a confirmed fill is adopted using its actual ticket, size, price
  and protection terms, while an unknown outcome stays blocked and is never
  resubmitted.
* Close intents are likewise fsynced with a unique request id before the live
  venue is called. An unknown close survives restart and can only be resolved
  from that exact durable fill; broker position changes alone do not book a
  close.
* **Recovery** either restores the exact account, symbol/instrument terms,
  strategy evidence, risk policy, durable entry fills, guardian state and full
  position-management state, then verifies broker tickets, volumes, entry
  prices, stops and targets; or leaves the run in `RECOVERY_REQUIRED`. It does
  not adopt a partial or mismatched snapshot.

## What is **not** built yet (and blocks a live run)

1. **No admitted AX-SCALPER candidate exists.** The run refuses to arm without
   one (`no_admitted_candidate`). Submission therefore stays blocked on a real
   account, exactly as the admission policy requires. Nothing in this increment
   fabricates evidence, costs, limits or a candidate.
2. **The run is not yet wired into the workstation.** There is no Start/Stop
   control on `/live` for it, no assembly in `build_live_workflow`, and no
   background pump thread: `AutonomousScalper.on_tick` is driven by its caller.
   The existing `/live` page and its actions are unchanged, so nothing in the UI
   claims this lane is running.
3. **Existing-position SL/TP changes use the guarded route.** `PositionBook`
   writes the exact stop/target intent durably before calling `BrokerVenue`,
   which resolves one exact ticket and dispatches through `LiveLeg` blockers,
   LIVE confirmation, the router and the account-bound MT5 adapter. A broker
   retcode is not confirmation: both requested protection fields must be read
   back from that ticket, and the confirmed result is durably committed. An
   unknown outcome remains pending and non-resendable; restart recovery accepts
   only the exact ticket/size/entry/stop/target state. This internal route is not
   wired to a workstation control.
4. **Booking a fill back into the managed book** depends on the broker naming
   the ticket; when it cannot (the fake terminal's record does not), the trader
   reports `protective_orders_not_verified` and the existing guard refuses
   further entries. That is fail-closed, and the test asserts it explicitly.
5. **Windows / MT5 have not been exercised.** No terminal, account or order was
   touched during this work; every test runs on `tests.fake_mt5.FakeMT5`.

## Tests

The fake-terminal suite covers REAL-account verdicts without contacting a
broker; guarded LIVE mode/arming/account gates; action-specific SLTP handling;
exact-position identity; read-back-only confirmation of both fields; failure
before durable pre-send intent; post-send ambiguity and idempotency; restart
reconciliation for stop and target changes; and shutdown without implicit
flattening. See `tests/test_mt5_adapter.py`,
`tests/test_scalper_execution_positions.py`, and
`tests/test_scalper_live_autonomous.py`.

PAPER remains isolated from the live lane. `PaperVenue` now tracks its own
open volume so a simulated partial close preserves the remaining position's
protection, and the trader still refuses a live venue unless the caller
explicitly opts in *and* the venue declares `live_execution`.
