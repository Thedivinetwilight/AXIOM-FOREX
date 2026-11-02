# Overnight paper-run checklist

For leaving the paper test running unattended, and for the morning after. Every
item is a check you can make from the workstation; none of them needs a terminal
command, and none of them can send an order.

The paper test fills **locally**, from the terminal's own quotes, into
`data/catalog/paper/catalog-paper-ledger.jsonl`. It cannot arm, size, or send
anything: the only surface it is given is read-only (`symbol_names`,
`symbol_info`, `tick`, `rates`).

---

## Before you leave it

| # | Check | Where | What "good" looks like |
| - | ----- | ----- | ---------------------- |
| 1 | **PAPER mode** | top banner on every page | `PAPER`. If it says `LIVE`, stop and read the mode page before anything else — a live session is a different thing entirely. |
| 2 | **Terminal connected, and which account** | Start page → *MetaTrader 5* tile | `Connected` with the account name you expect. The account shown is the **REAL** account: it is read-only here and its balance/equity are never used in a paper figure. |
| 3 | **Quotes fresh** | Start page → *Market data* tile; Performance page header chip | `FRESH` / `QUOTES FRESH`, with a quote time within the allowed age (default 120 s). The tile names the newest quote and its age. |
| 4 | **Paper session running** | Start page → *Paper session* tile | `RUNNING`, with a cycle count that has moved since you last looked. |
| 5 | **Screened rows to follow** | Start page → step 5 | At least one row ticked. With none, the session cannot start and says so. |
| 6 | **Starting capital set** | Paper performance | A local simulated amount and currency. Without it, cash/equity/return read `Unavailable` — AXIOM will not guess one. |
| 7 | **No broker orders** | Positions page → *Paper strategy runner* card | `order sending: no`, `live orders 0`. Paper fills are never broker confirmations. |

## While it runs (once, a few minutes later)

| # | Check | Where | Good |
| - | ----- | ----- | ---- |
| 8 | **Cycles advance** | Start page *Paper session*, or Activity | The cycle count is higher than when you started it. A frozen count with `RUNNING` means the loop is stuck; check *Activity* for the last event time. |
| 9 | **Ledger totals agree** | Start page *Simulated orders and fills* vs Performance | The same numbers on both: `N filled` equals `entry fills + exit fills`, and `open positions` equals the rows on the positions page. They are one reading, not two. |
| 10 | **Freshness still true** | any page | The quote age is still inside the allowance. It is judged against the clock when the page loads, so a page left open does not go stale on its own. |
| 11 | **Activity is appending** | Activity page | Events carry times from the last few minutes; the newest event is not from hours ago. |

## If data turns stale

Symptom: the *Market data* tile reads `STALE · PAUSED`, or a page shows
`PAUSED · STALE QUOTE` with a symbol, an age and a quote time.

* **Nothing is broken and nothing is lost.** The pause is the safety behaviour:
  the loop refuses to fill on a price it cannot vouch for, records a
  `stale_quote` event, and waits.
* Open positions stay open. Their marks show as unavailable rather than being
  priced from an old quote, so unrealised P&L and equity read `Unavailable`
  until a fresh quote arrives.
* Check, in order: the terminal is still open and logged in; the market for that
  symbol is not closed (the Activity page records `market_closed` after a long
  quiet); the quote feed has not been left on a weekend/holiday symbol.
* When a fresh quote arrives, the pause clears by itself and decisions resume.
  You do not need to restart the session.
* If it stays stale for the whole session, stop the test. Do not “fix” it by
  restarting repeatedly: repeated cycles on one stale feed record stale skips,
  not trades.

## If state recovery fails

Symptom: starting the test returns a blocker, and the page says *the paper test
will not resume* with a named position and reason. The state file has **not**
been modified.

This means a stored position is missing something the exit rules read — for
example the ATR a trailing stop is measured in, an entry price, or a direction —
or its watched row is no longer in the paper set. AXIOM refuses rather than
substituting a zero or inventing a stop, because a fabricated level would exit
somewhere the real run never would and the ledger would record it as a trade.

What to do, in order:

1. **Read the blocker.** It names the symbol and the exact reason.
2. **Note the ledger.** The closed trades and the entry fills are all still in
   `catalog-paper-ledger.jsonl`; the performance page reads them without a
   terminal. Nothing needs recovering to keep the history.
3. **If the position should not be carried over:** use *Simulate closing open
   positions* on the paper page. That is a simulated close at the current quote,
   written to the ledger like any other exit, and it is your explicit decision —
   the test never does it by itself to tidy the book.
4. **If the watched row is gone:** the run was restarted with a different paper
   set. Either restore that row to the set, or flatten as in step 3.
5. **Only then** start the test again. Do not edit the state file by hand unless
   you know the missing value: a guessed ATR is a guessed exit.
6. Keep the old state file. It is the evidence of what could not be resumed, and
   `recovery` in it is what the page reads.

## What this run cannot tell you

* It is a **paper** run: fills are local, and no order reached a broker.
* Commission and swap are only subtracted when your broker publishes the terms.
  If it does not, net P&L reads `Unavailable` — by design, never zero.
* Results are not a profitability claim, and one night of paper fills is not
  evidence that a candidate works: **0 of the 1,566 declared candidates have been
  tested against real market history** in this build's shipped state.
