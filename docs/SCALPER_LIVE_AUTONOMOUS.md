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
* An **unknown** submission pauses the run and latches the venue: no new order
  is submitted until `query()` proves the outcome from the durable record.
* **Recovery** either restores the exact snapshot (symbols, strategy
  fingerprint, mode, every open position with a readable protective stop) or
  leaves the run in `RECOVERY_REQUIRED` with the reconciliation path in words.
  No position is adopted from a partial record.

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
3. **No stop-modification route exists** on the guarded path, and the venue says
   so (`venue_modification_unsupported`) rather than pretending. Trailing and
   break-even management therefore report what they could not do; the
   broker-side stop attached at entry is the protection that exists.
4. **Booking a fill back into the managed book** depends on the broker naming
   the ticket; when it cannot (the fake terminal's record does not), the trader
   reports `protective_orders_not_verified` and the existing guard refuses
   further entries. That is fail-closed, and the test asserts it explicitly.
5. **Windows / MT5 have not been exercised.** No terminal, account or order was
   touched during this work; every test runs on `tests.fake_mt5.FakeMT5`.

## Tests

`tests/test_scalper_live_autonomous.py` — 26 tests: REAL acceptance; DEMO,
CONTEST, unclassified, unconfirmed, wrong-server, wrong-login and
wrong-fingerprint refusal; the leg enforcing it before every order and a changed
account stopping the lane; venue submission through the guarded leg, partial
fill, refusal before the broker, unknown-outcome latch, duplicate id; the
trader's refusal of an unannounced live venue; no admission → no arm; demo
account → no start; stale quote → no decision; one admitted signal → exactly one
order for the broker symbol; the trader refusing to call a position protected
when the broker record cannot show a stop; stop blocking entries; recovery
refusing state-less and mismatched resumes.

PAPER is untouched: `PaperVenue`, `ScalperDesk` and their guards are unchanged,
and the trader still refuses a live venue unless the caller explicitly opts in
*and* the venue declares `live_execution`.
