# AX-SCALPER — paper-trading runbook and known limitations

This is the operator document. It says how to start a paper session, what the output
means, how to stop safely, what a restart does, and what this build has **not**
demonstrated.

AX-SCALPER is paper-only. There is no live venue adapter in this package, the
configuration refuses any mode other than `PAPER`, and `axiom_forex/brokers/mt5/orders.py`
is not imported anywhere under `axiom_forex/scalper/`. A runtime test asserts both.

## 1. Before you start

```bash
python -m pip install "pytest>=8,<10"     # for the tests only
python -m pytest tests/test_scalper_*.py -q
```

Expected on the branch this document ships on: all scalper tests pass, and the full
repository suite passes. If either is not true, stop.

Nothing in this runbook connects to MetaTrader 5. The terminal-backed runner is **not
part of this build** (see §7); a session reads synthetic ticks, a tick file you
provide, or the lab's training replay.

## 2. Start a paper session

```bash
# synthetic ticks, probabilities calibrated on a separate training replay
python scripts/scalper_paper.py --source sample --ticks 400 --calibrate

# your own JSONL tick file (bid/ask/at per line), with a declared base rate
python scripts/scalper_paper.py --source file --file ticks.jsonl --prior 0.66

# the lab's training replay, no probability source: every signal will be refused
python scripts/scalper_paper.py --source train --ticks 300
```

Every run prints the mode, the **data class** (`SAMPLE_DATA` or
`HISTORICAL_TICK_REPLAY`), where the probabilities came from, and the instrument terms
with their source label (`SAMPLE_DECLARED_TERMS` means "declared for this lab", never
"read from a broker").

The third form is worth running once. A desk with no probability evidence refuses
every signal for want of samples; that refusal is the fail-closed behaviour working,
not a fault to route around.

At the end of the session the desk:

1. closes anything still open at the last quote (`paper_session_end`),
2. seals the ledger,
3. appends it to `--out` (`scalper_paper_ledger.jsonl` by default),
4. re-verifies the file **from disk** including the seal.

## 3. Reading the output

| Line | Meaning |
| --- | --- |
| `ticks … refusals` | what the tick core accepted, and every refusal by reason |
| `opportunities` | strategy-window evaluations. An opportunity that became nothing is still an opportunity |
| `signals` | setups that met a family's rule |
| `EV accept / reject` | how many survived the cost-aware, uncertainty-aware gate, and why the rest did not |
| `sizing` | how many produced a legal size, and why the rest did not |
| `risk` | guardian refusals by reason (concentration, correlation, drawdown, …) |
| `orders / fills` | submissions and confirmed fills |
| `trades` | closed positions with costs attached |
| `chain` / `sealed file` | ledger integrity, recomputed separately from the writer |

Two honest readings of a quiet session:

* **Zero trades is a result.** It means the gates refused everything; the funnel says
  which gate and why. Reporting "no trades" is not a failure to tune away.
* **A high win rate on synthetic ticks is not an edge.** The generator's drift is the
  most favourable possible world for a momentum rule. `docs/SCALPER_LAB_REPORT.md`
  reports those numbers next to that sentence for a reason.

## 4. Stop, flatten, recover

These are the only supported controls. Each has a defined effect, and none of them
touches a position that the operator has not asked about.

| Situation | Call | Effect |
| --- | --- | --- |
| Suspend new entries now | `desk.emergency_stop(reason="…")` | entries off; positions keep their stops and keep being managed; returns a `ShutdownPlan` |
| Close everything | `desk.flatten_all(reason="…")` | closes every position it has a current quote for; positions without a quote stay open and are recorded as `no_quote_available` |
| Ambiguous order | `desk.resolve_unresolved()` | queries the venue for each unresolved request id; never resends; resumes entries only when nothing is left unresolved |
| Re-arm entries | `desk.clear_emergency(operator="<name>")` | requires an operator name, which is written to the ledger |
| Compare books | `desk.reconcile()` | orphans, vanished tickets, unprotected tickets, trader/v venue disagreement |

An emergency stop **never** closes a position, and it never disables management. A
runner stop or a mode change never closes a position either. If the process dies, the
positions are still at the venue with their stops; the next session re-reads the
sealed ledger and the venue, and re-verifies protection before trading.

## 5. What the ledger contains

One append-only hash-chained JSONL stream per run, sealed at the end. Kinds include
`opportunity`, `signal`, `probability`, `ev_evaluation`, `rejection`, `risk_decision`,
`order_request`, `execution_result`, `position_opened`, `position_modification`,
`position_exit`, `pnl`, `data_quality`, `system_error`, `limit_change_request`,
`approval` and `state_change`.

Losers are recorded with the same ceremony as winners. A trade's `won` flag is the
**net** sign, so a gross-positive, net-negative trade is a loss in the ledger and in
every count derived from it.

## 6. Changing a limit

Limits are not editable at runtime. `Guardian.request_limit_change(...)` refuses with
`limits_are_not_agent_configurable` and writes the attempt to the ledger. A real
change is a configuration change, a review, and a restart — which is the only process
in which a human sees it.

## 7. Known limitations

### Not implemented, by design

* **No live order path.** No MT5 venue adapter exists; `ScalperVenue` is the contract
  one would satisfy. `fixture_config(mode="LIVE")` raises, and a non-paper venue is
  rejected in the trader's constructor.
* **No terminal-backed runner.** The read-only polling loop that would feed
  `desk.on_tick` from a terminal exists as a design (documented in
  `docs/SCALPER_ARCHITECTURE.md`), not as a script. Until it exists, a session runs on
  synthetic ticks or a tick file. This is a named blocker, not an oversight.
* **No MQL5 event handler.** The Python polling benchmark in `ticks.benchmark_ingest`
  measures local processing throughput only; no event-driven alternative was proposed
  or measured, so no recommendation is made.
* **No order-book model.** The paper venue prices from the quote plus declared
  slippage. Depth, queue position and latency-dependent adverse selection are absent.
* **No partial-close at the venue.** A partial fill is booked at the filled size; the
  remainder is not chased. A partial exit is a book operation, not a venue amend.
* **No live arming flow.** There is no PAPER/LIVE switch to get wrong: only PAPER
  exists here.

### Measured, but only on synthetic data

* every metric in `docs/SCALPER_LAB_REPORT.md`;
* the ingest benchmark (a local hardware number, not a broker or terminal figure);
* the cost arithmetic, which uses **declared** terms — no commission schedule, swap
  rate or stops level from any broker was read.

### Unknown and treated as unknown

* the real spread distribution, slippage distribution and reject/partial rates at any
  broker;
* the terminal's delivery delay (`broker_clock_basis` mixes transport and clock skew);
* swap, which is declared `UNKNOWN` rather than zero;
* whether any of these rules has an edge. The SKEPTIC refuses to approve them, and the
  report says so per strategy.

## 8. Verified here, unverified elsewhere

**Verified in this sandbox, against fakes and replay:**

* tick validation, de-duplication, ordering, staleness, gaps, clock reset, replay;
* regime classification, including the three no-trade regimes and their refusal to
  expose any eligible family;
* all five signal families, including the refusal to use a flow proxy on a feed with
  no volume;
* Beta posteriors, calibration, EV rejection on incomplete costs, sizing refusals;
* guardian limits, entry suspension versus management, the emergency-stop plan, and
  the refusal to let an agent edit a limit;
* paper venue idempotency, injected rejects, partial fills, ambiguous outcomes, and
  recovery by query only;
* stop, target, break-even, trailing, time and stale-quote exits; protection
  verification and reconciliation;
* the hash-chained ledger, the seal, and truncation detection;
* the skeptic's walk-forward, PBO, deflated Sharpe, cost stress, and its refusal to
  approve;
* the desk end to end, including the funnel and the restart contract.

**Unverified, and not to be implied by any figure in this repository:**

* any MetaTrader 5 terminal (connection, symbol table, tick stream, account figures);
* real spread, commission, swap, partial fills or reject rates at a broker;
* the Linux launcher and `tzdata` behaviour on the Windows workstation;
* browser display, keyboard shortcuts, and session persistence across a real trading
  day;
* a live paper-forward session in real time — the "paper-forward" section of the lab
  report compares replays, not a running session.

## 9. Do not

* do not point this package at a live account — there is no adapter, and adding one is
  a separate, reviewed change;
* do not treat a paper ledger as a broker statement: paper capital and results are
  never blended with broker figures;
* do not remove a refusal to make a session "do something";
* do not retune a strategy against the evaluation window and then quote its numbers;
* do not present a sample-data figure as market history.
