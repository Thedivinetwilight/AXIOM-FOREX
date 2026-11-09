# AX-SCALPER — execution, protection and recovery

This document describes what the TRADER does with a signal, what it does when the
venue is unsure, and what a restart does and does not inherit. It is the operational
half of the design: the parts that decide whether an order becomes one position, zero
positions, or an incident.

**There is no live order path in this package.** `Trader` refuses any venue whose
`mode` is not `PAPER`, and `axiom_forex/brokers/mt5/orders.py` is not imported
anywhere under `axiom_forex/scalper/`. A regression test enforces both.

## 1. The submission sequence

Every submission runs the same ordered gates. Each gate writes its refusal reason to
the ledger with the request's idempotency key.

1. **Unresolved order in flight?** → `previous_order_outcome_unresolved`, no
   submission. One ambiguous order blocks all new entries.
2. **Already submitted with this key?** → `submission_already_in_flight`, no second
   order. The key is `fingerprint[:12]:tick_id[:12]:side`, so the same signal can
   never be sent twice.
3. **Instrument declared?** An unknown symbol → `instrument_not_declared`. The symbol
   is never inferred from a price or a request id.
4. **Quote fresh?** Age > `max_quote_age_ms` → `signal_quote_is_stale`.
5. **Signal within its TTL?** → `signal_expired`.
6. **Priceable stop?** A signal whose stop cannot be expressed in instrument points
   has `stop_price = None` → `protective_orders_not_verified`. The desk does not send
   an order it cannot protect.
7. **Guardian** → every account check, all reasons recorded.
8. **Broker terms pre-check** → market open, volume step/min/max, `stops_level`
   distance, stop and target on the correct side of the entry.
9. **Submit once**, with the stop and target attached.
10. **Verify protection** against the venue's own record, not the order's intent.

## 2. Outcomes

| Venue answer | What happens |
| --- | --- |
| Accepted, full | Position booked at the filled price and filled size. Costs are attached at entry. |
| Accepted, partial | The position is booked at the **filled** size. The remainder is not chased and not re-submitted. The partial is counted and written down. |
| Rejected | Recorded with the venue's reason. No retry; the signal expires. |
| Unknown outcome | Recorded, the request id stays unresolved, **entries are suspended**, and the order is resolved by query only. |

### The unknown-outcome rule

A timeout that the venue has not answered is not a failure and not a success. The
desk does the only safe thing:

* it does **not** resend — a late arrival of the first attempt would be a second
  position;
* it **queries** the venue by the same request id (`Trader.resolve`);
* when the venue confirms a fill, the fill is booked and the ledger records
  `recovered_filled`;
* when the venue says it never saw the order (`recovered_not_found`), the entry is
  released, and a resend is still an operator decision recorded in the ledger;
* while anything remains unresolved, `may_open()` is not consulted at all: the
  unresolved check happens first.

`retry_after_not_found()` exists for that operator path and reuses the same
idempotency key, so a duplicate cannot be created by the retry itself.

## 3. Protection

* The stop is placed **with** the entry, not after it.
* Verification reads `venue.protection(ticket)` and compares the venue's stop with the
  fill: a buy must have a stop below its fill, a sell above it. Anything else is
  `protective_orders_not_verified`.
* On a failed verification the trader **suspends entries** and reports the position as
  unprotected. It does not "fix" it by inventing a stop.
* The position manager re-verifies on every management pass, and a position that loses
  its protection suspends entries again (`positions.PROTECTION_MISSING`).
* A stop is never widened. Break-even and trailing moves are ratchets: a candidate is
  applied only if it improves the position.

## 4. Position management

The loop runs on every accepted quote, for every open position on that symbol, and it
runs **even when entries are suspended**:

| Condition | Action |
| --- | --- |
| Quote through the stop | close at the touched price (gap-through fills at the quote, not the stop) |
| Target reached | close at the target or better |
| Held ≥ `max_hold_ms` | time exit |
| Quote older than `stale_quote_exit_ms` | close rather than manage a position blind |
| Momentum flip | close (the entry condition no longer holds) |
| Progress ≥ break-even trigger | move the stop to entry ± one point, once |
| Progress ≥ trail trigger | trail behind the best price, ratcheting only |
| No protection | keep the position, suspend entries, write an error |

Partial exits, when configured, book the closed portion and leave the remainder
managed.

## 5. Reconciliation

`PositionBook.reconcile(venue_tickets)` and `Trader.reconcile(tickets)` answer three
questions with lists, never with a boolean alone:

* **orphans** — the venue holds a ticket the desk does not know: reported, never
  adopted silently;
* **vanished** — the desk thinks a position is open and the venue does not have it:
  reported, not quietly closed;
* **unprotected** — a ticket with no stop at the venue: a halt condition.

## 6. Restart and recovery

A restart is a fresh desk. Nothing is inherited because it was true five minutes ago:

1. A new `ScalperDesk` starts with **no positions, no fills and an empty ledger**
   (a test asserts this, because "it remembered" is the bug).
2. The previous run's state is read from its **sealed ledger** and from the venue —
   both, compared, and any disagreement reported as an integrity warning.
3. Every recovered position is **re-verified for protection** before anything else.
   A position whose stop cannot be confirmed suspends entries until an operator
   resolves it.
4. Open positions are never closed by a restart, a mode change or a runner stop. They
   stay visible with their tickets, stops and provenance.
5. A state file that cannot be parsed or whose chain does not verify is a **blocker**:
   the desk refuses to resume rather than trading on a book it cannot explain.

The ledger is append-only and hash-chained, and it is **sealed** at the end of a run.
A forward chain cannot detect truncation of its own tail, so a seal entry writes the
head hash and the entry count as the final line; `verify_jsonl(..., require_seal=True)`
reports `missing_seal`, `entries_appended_after_the_seal`, `seal_entry_count_mismatch`
and `head_mismatch`.

## 7. Cost accounting at the exit

Gross comes from the book (entry price, exit price, side, point). The desk subtracts a
`TradeCost` computed at entry from the **observed** spread plus declared slippage
(both sides) plus commission in points. The net figure is written once, in one place,
and the ledger records the cost, its completeness and any missing terms.

A trade that is gross-positive and net-negative is a losing trade. The ledger's
`won` flag is the **net** sign, because a report that counts gross winners while
showing net losses is a report that lies by arithmetic.

## 8. Failure modes the desk accepts as real

| Failure | Behaviour |
| --- | --- |
| Duplicate tick | rejected by content identity; no extra decision, no extra order |
| Out-of-order tick | rejected, not reordered into a decision |
| Stale quote | rejected; a held position is closed rather than managed blind |
| Gap / disconnect | `tick_gap` event; the window must refill before decisions resume |
| Clock reset | ordering baseline and window cleared; entries stay off for `resync_ticks` clean quotes |
| Corrupt quote | rejected with a counter; the feed is not repaired |
| Venue reject | counted, not retried |
| Unknown outcome | halt, query, resolve |
| Restart | nothing inherited; re-verify everything |

## 9. What is not implemented

* No MT5 venue adapter. `ScalperVenue` is the contract a future adapter must satisfy;
  the paper venue is the only implementation.
* No order-book or depth modelling: the paper venue prices from the quote plus declared
  slippage.
* No partial-close support at the venue: a partial fill is booked, a partial exit is
  recorded by the position manager, but the venue has no amend-volume operation.
* No live-mode arming flow: the configuration refuses `LIVE` at construction.
