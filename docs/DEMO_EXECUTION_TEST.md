# The one-shot DEMO execution test

An execution smoke test, on `/live`. It exists to answer one question and no
other:

> **Does an order AXIOM builds actually reach the MetaTrader 5 account I am
> signed into?**

It is not a strategy, not AX-SCALPER, not the continuous LIVE Autopilot, and it
is **no evidence at all** about profitability or about whether any strategy
should trade. It sends **one** order, to a **demo** account, and then it stops.

## What the operator does

1. Sign the MetaTrader 5 terminal in to a **demo** account and press
   **Connect** on the workstation.
2. On `/mode`, switch the execution mode to **LIVE** and complete its preflight.
3. On `/mode`, **arm** live trading — the adapter's arming step, with its own
   phrase. Nothing in the demo test can arm.
4. Make sure the **Broker-side stop** for the chosen symbol is set to the
   distance you intend (on `/live`). The demo test sends that stop and no other.
5. On `/live` → **DEMO execution test**, choose the broker symbol, BUY or SELL,
   the volume in lots, and the protective stop in points — every control shows
   the broker's own terms beside it — then type the confirmation phrase and the
   account signature.
6. Press **Send one DEMO test order**. Read the broker's answer.

## What it refuses, and where

Every check runs twice: once while the form is drawn (as a blocker with its fix)
and again at submission, because a page that has been open for ten minutes is
not evidence about the terminal.

| Refusal | Reason code | Meaning |
| --- | --- | --- |
| The terminal is not connected | `mt5_not_connected` | nothing can be sent |
| MT5 did not classify the account | `demo_account_mode_unknown` | an uninterpretable answer is never read as DEMO |
| The account is REAL or CONTEST | `demo_account_required` | this test only ever sends to DEMO |
| Axiom is not in confirmed LIVE mode | `mode_not_live` | the live order path does not run |
| The adapter is not armed | `live_not_armed` | the existing live path would refuse anyway |
| No symbol / side / volume / stop / phrase / signature | `demo_*_required` | nothing is defaulted or prefilled |
| The symbol is not in the setup or the terminal cannot verify it | `demo_symbol_not_verified` | no invented symbols |
| The volume is off the broker's grid | `demo_volume_invalid` | the broker's minimum, step and maximum are quoted |
| The stop is inside the broker's `stops_level` | `demo_stop_invalid` | the broker's own number is quoted |
| The stop is not the one that would actually be attached | `demo_stop_mismatch` | no silent substitution |
| The phrase or the account signature is wrong | `demo_confirmation_mismatch`, `demo_account_confirmation_mismatch` | one-shot confirmation |
| A test already ran | `demo_test_already_used` | one order per record |
| A request is in doubt | `demo_submission_in_doubt` | it is never re-sent |

## The order path it uses

There is exactly one:

```
DemoExecutionTest            builds one OrderRequest + GateContext
      │                      (strategy_id = "axiom-demo-execution-test")
      ▼
LiveLeg.submit_plan(demo_test=True)
      │                   blockers → PreTradeGate.evaluate → ExecutionRouter.route_live
      ▼
MT5TradingAdapter.submit     write-ahead record → order_check → order_send
```

`LiveLeg` is the class the production live lane already uses. With
`demo_test=True` it **replaces** the strategy-approval requirement with stricter
ones — the terminal must positively say DEMO, the request must carry the demo
test's own identity, and it may not carry a close intent. Every other guard is
unchanged and still enforced: LIVE and confirmed, adapter connected and armed,
the armed account identical to the current one, trading enabled, the symbol
specification still matching the broker's, no unresolved order, the ledger in
agreement with the terminal's positions, and a configured protective stop.

The request itself is honest: `strategy_id` names the smoke test, never a
strategy, and the durable record says `not_a_strategy: true`. Nothing in this
feature can make a strategy order look approved, and no strategy approval can
make this test look like a trade.

## One order, ever — the durable part

Before the leg is called, a record is written and committed:

```
axiom-demo-execution-test : demo-20260310T135000Z-301dfd0b
  status: SUBMITTING
  values: symbol, broker_symbol, side, volume, stop, terms snapshot, quote time
  account: mode, confirmed, fingerprint, server
```

The id is the request id the adapter writes ahead of `order_send`, and it is
short enough (31 characters) that the **broker's own order comment is the same
string** — so a reconciliation never has to unpick a truncated prefix.

* A second submit finds `FILLED`/`REJECTED`/`REFUSED`/`RECONCILED` and refuses
  with `demo_test_already_used`; it does not retry.
* An ambiguous answer (no terminal response, an exception, an unreadable
  outcome) is recorded as `UNRESOLVED`, the adapter is **disarmed**, and the
  adapter itself then refuses to re-arm while the order is in doubt.
* `Reconcile the same request` re-reads the durable order row by that id, plus
  the broker's own order list, and reports what it can prove. It never sends.
* Whatever the broker answered, trading is disarmed afterwards: a test never
  leaves the lane open behind it.

## What it does not do

* It cannot close, modify, or re-open anything — a close intent is refused
  before the gate is consulted.
* It cannot arm, switch modes, connect, or change your broker settings.
* It cannot be reached from any PAPER page; PAPER has no adapter and no route to
  one, and the page states that next to the form.
* If the order fills, **the position stays open on your demo account**,
  protected by the stop you chose. This page cannot close it; do that in the
  terminal, then reconcile.

## Tests

`tests/test_demo_execution_test.py` (23 tests) runs against
`tests/fake_mt5.FakeMT5` — no terminal, account, or broker is involved:

* a DEMO account accepts exactly one order, the order carries the stop, the
  durable record and the broker comment share one id, and arming is off after;
* REAL, CONTEST and unclassifiable accounts are refused before anything is
  queued, with the mode named;
* every missing field is named, and a volume/stop outside the broker's terms is
  refused with the broker's own numbers;
* a stop that is not the one in force is refused, not substituted;
* a wrong phrase or a wrong account signature is refused;
* a second submit is a duplicate; the broker is asked exactly once;
* an ambiguous outcome is never re-sent, the adapter refuses to re-arm, and
  reconciliation reads the same id;
* PAPER mode cannot send, the leg refuses a demo test carrying a strategy
  identity, and a demo test cannot close;
* `/autopilot` never renders the form, and `/live` renders it with its refusal
  reasons and fixes.
