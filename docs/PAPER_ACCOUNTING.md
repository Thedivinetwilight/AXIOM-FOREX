# Paper accounting and performance — one ledger, one set of numbers

This document describes how the workstation answers four questions that used to
have four different answers on four different cards:

1. how many simulated orders and fills are there?
2. how many positions are open, and which ones?
3. how old is the quote behind a mark, right now?
4. what is the paper P&L, and what exactly does it include?

All four are answered in one place, from one durable artefact: the paper test's
own ledger. No page recomputes any of them.

```
  terminal quotes ─┐
                   ├─► CatalogPaperTest ─► catalog-paper-ledger.jsonl  (append-only)
  strategy bars  ──┘                     └► catalog-paper-state.json   (the current book)
                                                        │
                          paper-capital.local.json ─────┤
                                                        ▼
                                        paper_runner/accounting.py::read_account()
                                                        │
                         ┌──────────────┬───────────────┼────────────────┐
                         ▼              ▼               ▼                ▼
                    dashboard      performance      positions         activity
```

## The contradiction this replaced

| Shown | Where it came from | What was wrong |
| --- | --- | --- |
| "5 orders · 0 filled" | `snapshot()["orders"]` = entry events, `snapshot()["fills"]` = close events | an entry that opened a position was never counted as a fill; two meanings shared one word |
| "10 open" beside four names | `len(positions)` vs `positions[:4]` | the count was right, the list was silently truncated |
| "fresh" at 15:40, ten minutes old at 15:50 | `_quotes_seen[symbol]["age_seconds"]` computed when the tick was read and echoed back | a stale verdict was re-rendered as a live one |
| a P&L figure | the strategy runner's in-memory `counters`/`_equity` | a different book from the ledger the fills were written to, and two unmodelled costs treated as zero |
| "1 closed trade" twice | `_close()` wrote a priced event *and* a summary record under the same event kind | one close could be counted as two |

## What the accounting module guarantees

* **An entry fill is a fill.** `counts.orders`, `counts.fills` and
  `metric_by_key["fills"]` are one number derived once: `entry_fills + exit_fills`.
  The two halves are also reported separately, so nothing has to infer which is
  which.
* **Open positions are paired, not counted.** Events are walked in file order per
  position key: an entry opens, a close closes. The open book is what is left, and
  it is compared with the state file. A position in one and not the other becomes a
  named integrity warning, and the position is still shown.
* **Freshness is judged now.** Each quote carries its own `quote_time`; the age is
  computed against the clock at the moment the page is built. A quote older than
  the allowance pauses trading decisions and the page says so, with the age and the
  time of the quote.
* **Marks use the closing side.** Longs are marked at the bid, shorts at the ask.
* **Unknown costs are unknown.** Commission and swap are `None` when the broker did
  not publish them; a net figure that needs them is marked unavailable, never zero.
* **Currency is never assumed.** A trade's P&L is in its pair's quote currency.
  Totals are only added when every trade shares one currency, and only compared
  with the configured capital when the currencies match. Otherwise the totals are
  unavailable and per-currency subtotals are shown.

## The figures, and how each one is computed

| Metric | Formula | Available when |
| --- | --- | --- |
| Starting capital | the value in `paper-capital.local.json`, typed by the operator | always configured or "not set" |
| Paper cash | `starting capital + realised price P&L` | capital set, totals addable, capital currency matches |
| Open position value | `Σ |mark| × contract size × lots` | every open position has a fresh mark and a recorded contract size |
| Paper equity | `cash + unrealised price P&L` | both of the above |
| Realised P&L (price) | `Σ (exit − entry) × direction × contract size × lots` over closed trades | every closed trade has a recorded price P&L |
| Unrealised P&L (price) | `Σ (mark − entry) × direction × contract size × lots` | every mark is fresh |
| Net P&L (after costs) | `realised net + unrealised` | commission and swap are both known |
| Return on capital | `(equity − starting capital) / starting capital × 100` | equity is available |
| Simulated orders / fills | `entry fills + exit fills` | always (0 is a real zero) |
| Open positions | positions opened and not yet closed, from the ledger | always |
| Closed trades | close events carrying both fill prices | always |

Price P&L includes whatever the two fill prices carried — the spread on the side
that filled at bid/ask, and the modelled slippage on the legs that used it. Each
close event records `exit_basis` (`BID_ASK` or `LEVEL`) and `costs_modelled`, so a
stopped-out trade is not described as having paid a spread it never paid.

Commission and swap are never simulated by the paper test. They are subtracted
only when the terminal's own symbol record could price them.

## Where the data lives

| File | Written by | Contents |
| --- | --- | --- |
| `data/catalog/paper/catalog-paper-ledger.jsonl` | the paper test, one line per event | fills, closes (with `exit_basis`, `trail`, `trail_extreme`, `entry_bar_at`), signals, stale-quote skips, state migrations, session start/stop |
| `data/catalog/paper/catalog-paper-state.json` | the paper test, rewritten each cycle | status, watch list, open positions with their management levels |
| `paper-capital.local.json` | the operator, on Paper performance | the local simulated starting capital and its currency |

All three are Git-ignored. None contains a credential, an account number, or a
broker balance, and none is read from an account.

## Restarting a paper run

A paper position is not a number on a page: it is a set of levels the next bar is
compared against. The state file therefore stores **everything the exit rules
read** (`state_view()`, schema version 3), not the page summary:

| Field | Why the exit logic needs it |
| --- | --- |
| `entry_price`, `entry_mid`, `lots`, `direction`, `entry_at` | the trade's identity, and the base every level is measured from |
| `atr` | the unit a trailing stop is measured in — a restored `atr=0.0` froze the trail |
| `bar_at`, `bar_index` | the last bar this position was managed on (when it was last compared against its levels) |
| `entry_bar_at`, `entry_bar_index` | the bar the trade **opened on** — immutable; the "not on the entry bar" guard on the trail trigger |
| `stop`, `target`, `trail` | the active levels, as they stand now |
| `stop_initial`, `target_initial`, `trail_initial` | what the trade opened with, before the trail ratcheted |
| `best` | the trailing extreme the trail ratchets from |
| `candidate_id`, `symbol`, `timeframe` | which rule set and which market manage it |
| `quote_currency`, `contract_size` | so a money figure means something without a terminal |
| `last_bar` (per watch) | so a restart does not re-decide the bar it stopped on |

The state is rewritten after every management decision, not only at entry and
close: a trail that ratchets in memory only would resume *looser* than the test is
honouring.

### Three outcomes, and never a fourth

1. **Resumed exactly.** A version-3 record with every field ⇒ identical
   management, identical subsequent exits (`tests/test_paper_restart.py`).
2. **Migrated, with a note.** An older record is resumed **only** where the missing
   field cannot move an exit, and the derivation is written to the ledger as a
   `state_migrated` event. For a version-2 record the missing `entry_bar_at` is
   derived from the last bar the position was managed on — by construction never
   before the entry bar, so the resumed trail cannot fire on the entry bar. For a
   candidate that does not trail by ATR, `atr` is never read, so its absence is
   harmless.
3. **Refused.** If the missing value *does* feed an exit (a live trail with no
   ATR, or a live trail with no bar it could ever have been armed on), or a record
   has no entry price, no direction, no lot size, or no watched row to manage it,
   the run is **blocked**: status `BLOCKED`, reason
   `paper_state_incomplete`, a `state_recovery_failed` event in the ledger, and
   the state file left byte-for-byte as it was found. The page shows a data
   integrity warning naming the position and the reason, and the affected figures
   are marked unavailable. Nothing is substituted, and no stop is invented.

The accounting read model surfaces the same thing: a state file whose `recovery.ok`
is false becomes an integrity warning on every page that reads the ledger, so a
blocked restart can never look like a flat book.

## When a trailing stop is allowed to act

A trailing stop is a level the next bar is compared against, so it needs two
things the page summary never had: the level *and* permission to act on it.

* **Seeded at the fill.** A candidate that trails seeds `trail = entry price` at
  entry; the ATR distance only accumulates as the trade makes new extremes.
* **Armed only after the entry bar.** The guard compares **bar stamps**, not
  indices (`entry_bar_at < this bar's stamp`). It used to compare
  `index > bar_index`, but `bar_index` was overwritten with the current index just
  before the comparison, so the guard was `index > index` — always false, and a
  trail could ratchet forever without ever closing a position. An unparseable or
  absent stamp arms nothing: the decision is delayed, never invented.
* **The ratchet reads the current bar's own range.** Long: `trail =
  max(trail, high − atr); best = max(best, high)`. Short: mirrored on the low.
* **Therefore one behaviour looks odd but is correct.** A trailing row whose
  first managed bar makes **no new extreme** has `trail == entry price`, and the
  next bar that trades at or through it closes the trade at the entry price:
  **0.0 P&L**, reason `TRAILING_STOP`. That is what the evaluation
  (`strategies/catalog/simulate.py`) does on the same bars. It is documented here
  rather than "fixed", because changing it would make paper results disagree with
  the numbers the candidates were selected on.
* **A stale quote neither triggers nor fabricates an exit.** No level is tested
  while the quote is outside the freshness allowance; the position stays open and
  the page shows the mark as stale, with the reason.


## What this is not

* Not a broker balance. The connected account's balance and equity are never read
  into these figures, and the page says so in two places.
* Not a profit claim. A paper fill is a number in a local file, priced from a
  terminal quote; it says nothing about a live result.
* Not a live route. Nothing in the accounting module can size, arm, or send
  anything, and the pages that render it offer no live action.
