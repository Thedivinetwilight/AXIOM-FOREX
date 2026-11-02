# Task 9 — paper accounting, truthful freshness, performance page, workstation redesign

This is the report for the task: *make the paper-trading numbers reconcile, judge
freshness against the clock, give the paper book a real performance page, and
make the workstation understandable — without touching the running operation.*

Everything below was done in the repository only. Nothing connected to a terminal,
read a credential, armed live trading, changed a live setting, or sent an order.

---

## 1. Root causes of the contradictory figures

| # | Symptom | Root cause | Where it was fixed |
| --- | --- | --- | --- |
| 1 | "5 orders, 0 filled" while five entries sat in the ledger | `snapshot()["orders"]` held **entry events** and `snapshot()["fills"]` held **close events**. One word ("fill") meant two things, so an entry that filled was never counted as a fill. | `accounting.py` derives `orders == fills == entry_fills + exit_fills` from one pass over the ledger; `catalog_paper.py` writes explicit `entry_fills`/`exit_fills` counters. |
| 2 | "10 open positions" beside a list of four names | The count was `len(positions)` and the list was `positions[:4]`. Both were "right"; only one was complete. | `read_account()` returns the complete list; the positions page paginates all of it and the start page **names** every grouped symbol (`"GBPUSD ×10, …"`) behind the count. |
| 3 | A quote shown "fresh" at 15:50 that was read at 15:40 | `age_seconds` was computed once, when the tick was read, and echoed back on every later render. A stale verdict re-rendered as a live one. | `QuoteMark` keeps `quote_time`; `age_seconds(now)` is evaluated **at page build time** against `WorkstationApp.clock`. A quote past `DEFAULT_MAX_QUOTE_AGE_SECONDS` (120 s) sets `trading.paused` with the age and the quote's own time. |
| 4 | Two different P&L figures | The dashboard read the *paper strategy runner's* in-memory counters; the fills were written to the *catalog paper ledger*. Two books, one word. | One read model (`paper_account.read_paper_account`) built once per request and rendered by every page; the runner's own card is labelled as a separate book and its counters are not mixed in. |
| 5 | A P&L that treated unmodelled costs as zero | No cost stack at all. | Commission and swap are only subtracted when the terminal published the symbol terms; otherwise the net figure is **`Unavailable`** with the reason, never `0.00`. |
| 6 | One close counted as two trades | `CatalogPaperTest._close()` wrote both the priced close and a summary record under the **same** event kind (`simulated_close`), so pairing saw two closes. | New event kind `EVENT_CLOSE_RECORDED = "close_recorded"` for the summary; `_pair_events()` only counts a close that carries a fill price (`_is_priced_close`), and counts the rest in `counts["close_notes"]`. Old ledgers written by the previous build are read correctly. |
| 7 | The dashboard and the performance page could disagree | Each page called a different helper. | Both render the *same* `metric_by_key` dictionaries. A test asserts the exact values on both pages, not substrings. |

| 8 | **A restart changed a position's management** | `_restore_state()` rebuilt each open position from the *page-shaped* summary in the state file: `atr=0.0`, `bar_index=0`, and whatever stop/trail happened to be there. The ATR is the unit a trailing stop is measured in, so a resumed position trailed from a frozen level — and the trail also only reached the file at entry/close, so a restart could resume a *looser* stop than the running test was honouring. `_last_bar` was not restored either, so a restart re-decided the bar it stopped on and could act on a signal the uninterrupted run had already spent. | `state_view()` + schema version 2 (every field the exit rules read), a rewrite after every management decision, `_last_bar` restored, version-1 migration with a stated derivation, and a fail-closed refusal when a record cannot be resumed exactly. |

---

## 2. Files changed

New:

| File | Lines | Purpose |
| --- | --- | --- |
| `axiom_forex/paper_runner/accounting.py` | 1490 | The single accounting read model: ledger pairing, marks, realised/unrealised/net, currency handling, freshness, integrity problems, metric definitions, capital document reader/writer, formatters. |
| `axiom_forex/workstation/paper_account.py` | 128 | `paper_root()` / `read_paper_account()`: builds that model once per request from the panel plus the clock. No arithmetic, no formatting. |
| `tests/test_paper_accounting.py` | 1130 | 43 targeted regression tests (§5). |
| `docs/PAPER_ACCOUNTING.md` | 102 | The design note: ledger → read model → pages, with every formula. |
| `tests/test_paper_restart.py` | 755 | Restart/restore regression: uninterrupted vs resumed, migration, fail-closed refusals. |
| `docs/OVERNIGHT_PAPER_RUN.md` | 91 | The overnight checklist: mode, terminal, freshness, orders, cycles, totals, and what to do when data goes stale or recovery fails. |
| `docs/TASK9_REPORT.md` | this file |

Modified:

| File | Change |
| --- | --- |
| `axiom_forex/paper_runner/catalog_paper.py` | Ledger records carry `quote_bid`/`quote_ask`/`quote_time`/`priced_from`, the fill's `exit_basis` (`BID_ASK`/`LEVEL`), `costs_modelled`/`costs_not_modelled`, contract size and quote currency; explicit entry/exit fill counters; `EVENT_CLOSE_RECORDED`; age recomputed at read time. **State schema version 2**: `Position.state_view()` persists every field the exit rules read (ATR, bar index and bar stamp, active and initial stop/target/trail, trailing extreme, mid, currency, contract size); the state file is rewritten after every management decision; `_last_bar` is restored so a restart does not re-decide the bar it stopped on; version-1 records are migrated with a stated derivation or refused; `REASON_STATE_RECOVERY`, `EVENT_MIGRATED` and `EVENT_RECOVERY_FAILED`. |
| `axiom_forex/strategies/catalog/panel.py` | `paper_root()`, `paper_snapshot()`, `cost_model()` so the workstation reads the panel's real paper folder and the terminal's own cost terms. |
| `axiom_forex/workstation/app.py` | `GET /performance`; `POST /actions/paper-capital`; `WorkstationApp.paper_account()` and a replaceable `clock`; `extra["account"]` in the page context. |
| `axiom_forex/workstation/views.py` | New `NAV`/`NAV_GROUPS` (essential first, advanced grouped), `render_performance`, rewritten `render_positions`/`render_activity`/`render_start`, the four-action row, the REAL-vs-PAPER strip, the freshness chip, pagination (25 rows/page), the definitions card, the capital setup form. |
| `axiom_forex/workstation/guide.py` | The start page's tiles are fed by the same account reading (`_order_tile`, `_position_detail`, `_performance_tile`, `_account_market_data`). |
| `tests/test_workstation_start.py`, `tests/test_workstation_ui.py` | Nav grouping/icon completeness; exact POST allowlist. |
| `tests/test_quick_setup_ui.py`, `tests/test_catalog_autopilot.py` | Stale expectations, not product changes: the nav label is now "Broker terms"; the Kronos gate tests pin the clock instead of reading the wall clock. |

---

## 3. The metrics, and the formula behind each

All of these are computed once, in `accounting.py`, and rendered from the same
dictionaries everywhere.

| Key | Label | Formula | Becomes `Unavailable` when |
| --- | --- | --- | --- |
| `starting_capital` | Starting capital (local simulated) | the operator's typed amount + currency | not configured (never guessed) |
| `cash` | Paper cash | `starting capital + realised price P&L` | capital unset, totals not addable, or currency mismatch |
| `open_value` | Open position value (marked) | `Σ |mark| × contract size × lots` | a mark is stale/absent or a contract size is unknown |
| `equity` | Paper equity | `cash + unrealised price P&L` | either input is unavailable |
| `realized_gross` | Realised P&L (price) | `Σ (exit − entry) × direction × contract size × lots` | a closed trade has no recorded price P&L, or totals span currencies |
| `realized_net` | Realised P&L (after costs) | `realized_gross − commission − swap` | either cost is unknown |
| `unrealized_gross` | Unrealised P&L (price) | `Σ (mark − entry) × direction × contract size × lots`, long at **bid**, short at **ask** | any mark is stale or missing |
| `net_pnl` | Net P&L (after costs) | `realized_net + unrealized_gross` | either part is unavailable |
| `return_pct` | Return on starting capital | `(equity − starting capital) / starting capital × 100` | equity unavailable |
| `open_positions` | Open positions | positions opened and not yet closed, paired event by event | never (0 is a real zero) |
| `closed_trades` | Closed trades | close events carrying both fill prices | never |
| `orders` | Simulated orders | `entry_fills + exit_fills` | never |
| `fills` | Simulated fills | the same expression — identical by construction, not by coincidence | never |

Currency rules: a trade's P&L is in its pair's **quote** currency. Totals are
summed only when every trade shares one currency (`realized_by_currency` gives the
subtotals either way), and compared with capital only when that currency matches.
Otherwise the aggregate is unavailable and says which currency broke it.

Costs: the spread and the modelled slippage are *inside* the two fill prices and
are declared per close as `costs_modelled`. Commission and swap are **not**
simulated; they are subtracted only when the symbol's own terms could price them.
`costs.complete` is true only when nothing is missing — otherwise the page says
`cost-incomplete` and names the cause ("commission (no symbol terms were
readable)").

---

## 4. What was built for each part of the brief

**A. Reconciliation.** The ledger is authoritative. Positions are paired from
events (not counted from a truncated list) and cross-checked against the state
file; a state-only position is shown *and* flagged. Any anomaly — a broken ledger
line, an unpaired close, a currency that cannot be added — becomes a named entry
in `integrity.warnings`, rendered as a warning banner, and the affected metrics are
marked unavailable with their reason. Nothing is hidden and no total is
hard-coded. Both long lists are complete and paginated (25/page, with "showing 26–50
of 137").

**B. Freshness.** Quote age is judged against the page-build clock. Stale ⇒
`Trading decisions PAUSED`, with the offending symbol, its age and its quote time,
and the allowance. The dashboard shows `NO QUOTE YET` / `PAUSED · STALE QUOTE` /
`QUOTES FRESH`. When no live quote exists, the ledger's *last fill* quotes are used
as marks and are labelled as historical marks with their age, never as live prices.

**C. Paper performance.** `/performance` shows: configured starting capital and
currency (labelled local simulated), paper cash, marked open-position value, paper
equity, realised price P&L, realised after costs, unrealised, net after costs,
return %, open positions, closed trades; a **dated** cumulative P&L chart (one row
per UTC day that closed a trade — no line is drawn before the first trade); the
complete trade history (symbol, strategy family and candidate id, side, lots, entry
time and price, exit time and price, what closed it plus whether that leg filled on
the spread or at a level, price P&L, net, cost status); the latest-update time; the
data-source label and the two file paths; the definitions table; and a costs card.
Empty ledger ⇒ a helpful empty state with **no sample profits**. Capital unset ⇒ a
two-field form (amount + currency) and a page that refuses to guess a balance.

**D. Workstation.** Nav is grouped: *Paper trading* (Start, Performance, Positions,
Activity), *Set up*, *Data & evaluation*, *Strategy library*, then *Advanced*
(Quick setup, Broker terms, Trading mode, Live execution, Setup steps, Autopilot,
Kronos). The home page answers, in order: terminal connected?, paper running?,
quotes fresh?, did the strategy signal?, were there simulated fills?, what is the
paper P&L? — and offers **Connect / Start paper / Stop paper / View performance** in
one row, each either working or disabled with the plain reason printed beside it.
The `REAL BROKER` / `PAPER` split callout appears on the home, performance,
positions and activity pages; the connected account is always described as
read-only and its balance/equity as never read into a paper figure. `/live` now
opens with a warning banner *before its first form* that states the consequence,
that paper is the default, and that nothing there connects, switches account or
edits settings. No live control appears on any paper page, and live settings and
defaults are unchanged.

---

## 5. Verification

Command: `PYTHONPATH=. python -m pytest <paths> -q` (run from the repository root)

| Run | Result |
| --- | --- |
| `tests/test_paper_accounting.py` | **46 passed** (0.4 s) |
| Focused paper suite (`catalog_paper`, `catalog_panel`, `catalog_view`, `catalog_autopilot`, `workstation_start`, `workstation_ui`, `forex_paper_runner`, `paper_provider_lane`, `autopilot_ui`, `quick_setup_ui`, `paper_accounting`, `paper_restart`, `live_workflow`) | all passed |
| `tests/test_paper_restart.py` (new) | **16 passed** |
| Whole repository `tests/` (final) | **1716 passed** (412 s) |

Coverage added (targeted, no new harness): entry-fills counted as fills; five
entries read as five filled; position count == position list, including a book
longer than a page; a ten-minute-old quote reported as such and flipping to stale as
the clock moves without a re-read; long marked at the bid and short at the ask;
realised P&L summed in the recorded currency; unknown cost ⇒ `Unavailable`, never
zero, and a cost-complete case when the broker publishes terms; an unpriced trade
unavailable; two quote currencies ⇒ totals unavailable with subtotals; capital in a
different currency not added to P&L; a broken ledger line as a named warning; a
state-only position flagged; unset capital leaving cash/equity unavailable; bad
capital refused (parametrised); write/read round trip; the capital action saving and
refusing; empty ledger; dashboard == performance on exact values; the perf page's
required metrics and source; the perf page not mixing the broker account; every
`GET_ROUTES` page rendering with a real account reading; the capital form only when
missing; activity pagination not dropping rows; the four key actions with their
disabled reasons; stop-in-the-same-row while running; the live page warning; the
reported "10 orders / 0 filled / 10 open across five symbols" triple rendered through
the real pages; a stale-then-fresh clock flip on one unchanged ledger; and a blocked
restart surfacing as a data-integrity warning.

Also added, end-to-end against the **real** `CatalogPaperTest` writer from
`tests/test_catalog_paper.py`: the ledger it writes is readable without a terminal
(contract size and currency from the ledger, `mark_fresh is False`,
`unrealized_gross is None`); a closed trade reconciles to the ledger's own
`simulated_close.simulated_net_quote`; a ledger written under the old event kinds is
not double counted; and the writer now emits exactly one `simulated_close` plus one
`close_recorded` per close.

Rendered sanity: `/`, `/performance`, `/positions`, `/activity`, `/live` were built
from a populated fixture ledger and inspected with the CSS stripped — figures agree
across pages (`10,069.00 USD` equity = `10,050.00` cash + `19.00` unrealised; `3`
simulated fills = `2` entry + `1` exit; `1` open), unavailable figures carry their
reason, and the REAL/PAPER callouts, timeline and pagers are present.

### Verified in the sandbox vs still unverified

Verified here, with fake MT5/fixture data and temporary ledger folders:

* the accounting arithmetic and every unavailable/currency/cost rule;
* ledger pairing, the double-close fix, and old-ledger compatibility;
* freshness evaluation against an injected clock;
* every page renders, paginates, and shows the same numbers;
* the POST allowlist, nav grouping, capital form validation;
* no page reaches a live action or an `order_send` (the fake module is the only one
  imported by the tests).

Restart coverage (`tests/test_paper_restart.py`, 16 tests, all fixture data):

* an uninterrupted run and a stop/restart/resumed run over the same 60 bars produce
  the **same** `simulated_fill`/`simulated_close` sequence, the same prices, the same
  simulated P&L, and the same `orders`/`fills`/`entry_fills`/`exit_fills`/
  `open_positions`/`closed_trades` through the accounting module — at two different
  restart points (one with a position open, one after a close);
* a restored position carries every field the exit rules read, field for field;
* the bar already decided on is not decided again after a restart;
* a trailing position's trail and extreme ratchet identically across a restart;
* a version-1 record is migrated with a stated derivation, keeps its stored levels,
  emits a `state_migrated` event, and continues to the same fills;
* a version-1 record whose *live trail* has no ATR is **refused**; so is a record
  with no entry price, a bad direction, or no watched row; a corrupt state document
  blocks the start; in every refusal the state file is byte-identical afterwards,
  no fill or close is written, and `state_recovery_failed` is recorded;
* an empty folder is not a failure.

Sensitivity was checked by experiment, not asserted: removing the per-decision state
write makes two of these tests fail (stale `bar_at`/`_last_bar` on resume), so the
tests do detect the class of defect they describe.

**Requires your MT5 session, not verified here:**

* that a real terminal's `symbol_info()` supplies commission/swap terms for *your*
  symbols (the sandbox has no broker terms, so the cost path always shows
  `cost-incomplete`);
* that real `symbol_info_tick()` quotes arrive often enough to stay inside the 120 s
  allowance on your pairs and session;
* real contract sizes, `stops_level`, and quote currencies per instrument;
* anything about behaviour on Windows with the packaged desktop launcher.

No profitability claim is made anywhere: the ledger has 0 closed live-market paper
trades in this environment, and the standing limitation still holds — **0 of 1,566
declared candidates have been tested against real market history**; the sandbox
catalog numbers are fixture numbers.

---

## 6. Remaining limitations

1. ~~**The trail trigger itself is inert, by an older design decision.**~~ **Fixed
   after this report** — see §7. The guard compared the current bar index with a
   field that was overwritten *with that same index* earlier in the same call, so a
   trailing stop could ratchet but never exit. The state schema is now v3 and the
   guard is a bar-stamp comparison against the entry bar.
2. **Commission and swap are never simulated.** They are subtracted only when the
   broker publishes them. On a broker that publishes nothing, every net figure is
   `Unavailable` by design.
3. **No currency conversion.** Two quote currencies ⇒ no aggregate. A conversion
   would need a rate source that is neither a broker balance nor a guess.
4. **The activity timeline is bounded.** The ledger keeps 60 events; the page says
   how many older events are on disk but not paged.
5. **The equity chart is per closed day**, not intraday, and it plots realised price
   P&L before commission and swap (stated under the chart).
6. **The paper strategy runner's own card is a separate book.** Labelled as such and
   explicitly excluded from the ledger figures; merging it would recreate the
   original contradiction.
7. **`snapshot()` is still truncated by design** for the session view; the complete
   lists come from the accounting read model.
8. **Unverified on Windows/MT5** as listed in §5.

## 7. Follow-up: the inert trailing stop (fixed)

The limitation above was a real execution defect, not a design choice, and it is
fixed in the paper path only:

* **Root cause.** `_decide()` writes the managed bar's identity onto the position
  (`bar_index = index`, `bar_at = …`) *before* `_manage()` runs, and `_manage()`
  then evaluated the "not on the entry bar" guard as `index > position.bar_index`.
  That is `index > index`: always false, in every cycle. The evaluation rule the
  paper path is meant to mirror (`strategies/catalog/simulate.py`, `index >
  entry_index`) was therefore never reached, so a trail ratcheted and never exited.
* **Fix.** Positions now carry the bar the trade *opened on* — `entry_bar_at`
  (stamp) and `entry_bar_index` (reference only) — recorded immutably at entry and
  never overwritten by management. The guard is
  `position.trail is not None and entry_bar_at < current bar stamp`; an
  unparseable or missing stamp arms nothing (delay, never fabricate). The exit
  itself still goes through the same level-basis exit, so stop/target/freshness
  protections are untouched.
* **State schema version 3.** Every record now persists `entry_bar_at` /
  `entry_bar_index`. A v2 record derives `entry_bar_at` from the last bar the
  position was managed on — a bar that is never before the entry bar, so a resume
  cannot fire on the entry bar — and records the derivation as a `state_migrated`
  ledger event. A v2 record **with a live trail but no managed bar at all** is
  refused: the trail could never be armed, and guessing an entry bar would invent
  an exit.
* **Close events** now also carry `trail`, `trail_extreme` and `entry_bar_at`, so a
  trailing exit is attributable after the fact.
* **Degenerate case, unchanged and intentional.** Because a trailing row seeds its
  trail *at the fill* and the ratchet reads the current bar's own high/low, a bar
  that makes **no new extreme** legitimately stops the trade out on the *next* bar
  at the entry price: 0.0 P&L, reason `TRAILING_STOP`. That is what the evaluation
  does on the same bars, so the paper path matches it rather than diverging; the
  fixtures in `tests/test_paper_trailing_stop.py` assert this exact behaviour
  instead of hiding it.
* **Coverage** (`tests/test_paper_trailing_stop.py`, 14 tests): ratchet-and-exit
  long and short; no exit on the entry bar; the runner's own entry surviving its
  entry bar and closing on the trail; restart parity (trail, extreme, ledger);
  v2 migration with a stated derivation and the refusal path; stale quotes neither
  triggering nor fabricating a fill; the ledger, trade list, closed-trade count and
  dashboard agreeing after a trailing exit. Reverting the guard fails 6 of them.

Verified in this sandbox with fake quotes and fixture bars only. **Not** verified:
real trailing exits on a live quote stream, a real terminal restart against a v3
state file, and a genuine stale-feed episode on Windows/MT5.

No commit, push or pull request was made from this report — the trailing-stop fix
is a separate follow-up commit on `arena/f67c2859-axiom-forex`.
