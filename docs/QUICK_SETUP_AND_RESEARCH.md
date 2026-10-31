# Quick setup, one year of history, and paper trading

This is the normal route through AXIOM's workstation: **Quick setup → one year of
history → one evaluation → paper trading**. Live execution stays a separate mode
with its own gates, and nothing on this route shortens it.

The point of the route is that most of what the old provider-terms page asked for
is not a *choice*. Your terminal already knows the account currency, the leverage,
the exact broker symbols, their digits, point, contract size, volume grid, stop
distance and, on most brokers, the swap terms. AXIOM reads those and asks you only
for what no terminal can know.

---

## 1. What the terminal answers, and what it cannot

| Read from the terminal (`OBSERVED`) | Asked of you (`OPERATOR`) | Stays `UNKNOWN` |
| --- | --- | --- |
| account currency, leverage, demo/real | symbols to trade | commission schedule |
| exact broker symbol, digits, point | lot size | broker session windows and holidays |
| contract size, min/max/step, stops level | max open positions | rollover cut-off hour |
| spread in points | max daily loss, max order notional | swap in interest-rate modes |
| swap long/short and its mode, when priceable | spread cap, research window | account↔quote currency conversion (when they differ) |

Every value on the page carries its provenance: `OBSERVED` (read from your
terminal), `SIMULATED` (the offline sample — never described as real), or
`UNKNOWN` with a sentence saying what that costs you. An unknown value is never
replaced by a zero, because "commission is zero" is a claim and it would make
every result look better.

Two of those unknowns matter differently:

* **Research** may run with clearly-labelled assumptions (slippage, commission)
  and reports them with the results, plus a cost-sensitivity run. It does not need
  the calendar.
* **Live execution** still needs the broker-terms document
  (`docs/LIVE_EXECUTION.md`, page **Broker terms (advanced)**, `/provider-setup`),
  the pre-trade gate, the risk limits, and the protective-stop rule. The short
  setup says so on the page instead of pretending otherwise.

## 2. Recording the setup

`/quick-setup` writes one local, Git-ignored file:

```
quick_setup.local.json     # beside your mt5_settings.local.json
```

It contains the observed facts, your five answers, your cost assumptions, and two
separate readiness verdicts. It contains **no credential, no login, no account
number**, and there is no field that could carry one. Existing local settings and
credentials are untouched: this file is new, and writing it reads nothing secret.

## 3. One year of history

Press **Read and store that history**. For each chosen symbol and timeframe the
workstation calls the terminal's own read-only `rates()` and stores the bars:

```
data/history/research/EURUSD_pro__1h.json
```

* Bars are **bid-only** in the terminal, which also reports the spread that
  applied to each bar. The research series is therefore *bid plus that bar's own
  quoted spread*, labelled `BROKER_RATES_BID_PLUS_QUOTED_SPREAD` in every result.
  It is a research series, not an execution-grade quote stream.
* Nothing is downloaded, generated, or substituted. If the terminal has no bars
  for a symbol/timeframe, the page says so.
* Every stored file records its origin. A sample or fixture series can only be
  loaded when a caller asks for a sample *by name*, so a fixture cannot quietly
  become a strategy result.
* Coverage is audited before anything is measured: bar count, span, gaps, steps of
  a day or more (market closures — labelled, not judged), and whether the history
  reaches the recent window. The page lists **exactly which symbols and
  timeframes are usable, and why the others are not**.

## 4. The one-year evaluation

Press **Run the one-year evaluation**. The sweep:

1. computes every predeclared variant's signal once over the stored series. The
   rules are the library's own; any bar whose comparison lands too close to a
   threshold to trust in floating point is re-decided by the real rule on real
   bars, and the count of those bars is reported;
2. fills at the **next bar's open** on the side the trade pays (ask for a buy, bid
   for a sell) plus the stated slippage, with the spread each bar itself quoted;
3. charges commission and swap only when they can be priced, and otherwise names
   them in `costs_incomplete`;
4. splits chronologically **60 / 20 / 20 — train / validation / test**. The
   selection is made on validation. The test window is then measured **once**, for
   the selected variant alone; every other row says it was not measured on test;
5. reports, per strategy, symbol and timeframe: trade count, gross and net
   return, maximum drawdown, the cost breakdown, and — for the selected variant —
   the out-of-sample result, later-chunk walk-forward folds, and a cost-sensitivity
   run at 1× and 2× the assumed costs.

Budget and honesty:

* the run has a **540-second wall-clock budget** (under the ten-minute
  requirement), shows progress, and can be cancelled;
* a budget stop or a cancel returns a partial result that **lists what did not
  run**, with a reason — a truncated run cannot be mistaken for a complete one;
* every measured attempt is written to the strategy ledger, failures included;
* the live quote and execution path is untouched: no quote is slowed, no tick is
  queued behind a research run.

**Nothing here is a profitability claim.** The results are historical estimates on
broker bars under the stated cost assumptions. A good-looking window is a reason
to keep looking.

## 5. Paper trading, one click

Once the setup is research-ready and you have **selected** a strategy on
`/strategies`, the page's paper card offers **Start paper trading**. That is the
existing runner action (`runner-start`) and nothing new:

* prices come from your terminal's **current** quotes;
* fills are simulated locally, in a durable local paper database;
* positions and activity appear on the workstation's Positions and Activity pages;
* starting it never changes the execution mode, never arms live, never sends an
  order, and refusing to start never leaves a half-configured runner behind.

If the runner was not configured at launch, the card says so and lists what is
missing instead of offering a button that cannot work. The launcher options are in
`docs/PAPER_RUNNER.md`.

## 6. Live execution is unchanged

The sub-setup does not relax anything. A live order still passes:

* the broker-terms document (commission, sessions, rollover, conversion),
* the pre-trade gate and the risk limits you set here,
* the account/risk/arming/confirmation checks on `/live`,
* the protective-stop requirement.

There is no path from this page to an order. The page has four actions —
`quick-setup-save`, `research-fetch`, `research-run`, `research-cancel` — and
`tests/test_quick_setup_ui.py` pins them.

## 7. On a Windows machine with your terminal

1. Sign in to your terminal (AXIOM never logs in, never switches accounts).
2. Start the workstation as usual and press **Connect** on Setup.
3. Open **Quick setup**, tick your symbols, fill the five answers, save.
4. Choose timeframes, press **Read and store that history**. Start with `1h`
   (about 6,200 bars) or `4h`; `1m` over a year is ~370,000 bars and takes longer
   to fetch and evaluate.
5. Press **Run the one-year evaluation** and read the results — including the rows
   saying what did not run.
6. Evaluate and select a strategy on `/strategies`, then start paper trading.
7. Only when you are satisfied, and only after the broker-terms document is in
   place, consider live execution on `/live`.

Expected first-run rough numbers on an ordinary desktop: 42 variants over one
year of H1 bars (≈6,200) finishes in well under a minute; over M1 (≈372,000) it
takes about a minute of signal work per symbol. The budget stops the run and says
what it could not reach rather than running past ten minutes.
