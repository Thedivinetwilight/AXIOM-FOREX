# AX-SCALPER — architecture and integration plan

AX-SCALPER is a second, **event-driven** trading path beside the existing
completed-bar engine. It does not replace, modify or bypass anything that already
exists: the conventional bar-based catalog/paper test, the strategy-library
runner, the live workflow and its gates are untouched and continue to work.

The one thing AX-SCALPER is allowed to *use* from the existing system is
infrastructure: tick access through the MT5 terminal object, the broker/account
models, the cost model, the multiple-testing primitives, the append-only audit
conventions and the paper-only boundary. It never imports an order-sending path.

```
                         ┌────────────────────────── AX-SCALPER (paper only) ──────────────────────────┐
  MT5 terminal (data)    │                                                                              │
  or tick replay ──────► │  AX-TICK CORE ──► AX-REGIME ──► SIGNAL GENERATOR ──► BAYES ──► EV CHECK      │
                         │        │                              │                     │               │
                         │        │                              └──────► AX-REGIME eligibility          │
                         │        ▼                                                                    │
                         │   data-quality gate ──────────────────────────────► AX-GUARDIAN (entry)     │
                         │                                                        │                     │
                         │                                                        ▼                     │
                         │   VARIANCE (size) ──► TRADER (paper venue) ──► POSITION MANAGER               │
                         │                                   │                     │                     │
                         │                                   └──────► SCRIBE ◄──────┘                     │
                         │                                                                              │
                         │   SKEPTIC (offline qualification) ──► approval artifact ──► desk may run      │
                         │   AX-LAB (replay, costs, walk-forward, report)                                │
                         └──────────────────────────────────────────────────────────────────────────────┘
```

## Why a separate path

The bar engine decides once per completed bar; it is correct for what it does.
A scalping decision needs to see every quote update, and needs the *cost* of an
entry to be part of the decision rather than a line in a report. Those are
different data flows, not better ones. Keeping them separate means a fault in
the fast path cannot corrupt a bar-based paper book, and vice versa.

## Modules

| Module | Role |
| --- | --- |
| `axiom_forex/scalper/config.py` | Frozen, validated configuration. `mode` may only be `PAPER`; construction refuses anything else. |
| `ticks.py` | AX-TICK CORE: validation, dedup, sequencing, gap/out-of-order/stale/spread detection, latency, windows, replay source, benchmark. |
| `regime.py` | AX-REGIME: deterministic classification and per-family eligibility; can always say NO TRADE. |
| `signals.py` | SIGNAL GENERATOR: five families with explicit entry/exit rules and declared parameters. |
| `bayes.py` | BAYES: Beta posteriors, calibration, uncertainty, flow estimate, broker-feed caveat. |
| `ev.py` | EV CHECK: expected net value in points and account currency, uncertainty-aware rejection. |
| `variance.py` | VARIANCE: risk-based sizing from stop distance, contract spec and exposure. |
| `guardian.py` | AX-GUARDIAN: immutable account-level limits, entry suspension separate from position management, emergency stop. |
| `execution.py` | TRADER: idempotent paper submission, partial fills, rejection, unknown outcome, latency/slippage. |
| `positions.py` | POSITION MANAGER: stop/TP/break-even/trailing/time/partial exits, reconciliation, protection verification. |
| `scribe.py` | SCRIBE: hash-chained append-only JSONL journal. |
| `skeptic.py` | SKEPTIC: chronological splits, walk-forward, multiplicity, PBO, deflated Sharpe, cost sensitivity, approval artifact. |
| `lab.py` | AX-LAB: tick replay, realistic fills, metrics, walk-forward, comparison, degradation detection. |
| `desk.py` | The orchestrator: wires the above, owns the paper loop and the state file. |

## Integration boundaries (what is reused, and what is not)

Reused:

* `axiom_forex.brokers.mt5.terminal.MT5Terminal.tick()` — **read-only data**.
  AX-SCALPER is handed a callable, never a terminal that can trade.
* `axiom_forex.research.engine.CostModel` — the same declared cost vocabulary.
* `axiom_forex.strategies.library.multiplicity` — BH, block bootstrap, Reality Check.
* `axiom_forex.monitoring.health` conventions for honest status lines.
* The repository's own append-only/no-rewrite conventions (`scribe.py`).

Not reused, deliberately:

* `axiom_forex.brokers.mt5.orders` — the live order path. It is **not imported**
  anywhere in `axiom_forex/scalper/`, and a test enforces that.
* The bar-based runner/feeds — different clock, different fill model.

## Data and clock discipline

* Every tick carries `source_time` (the broker's stamp), `received_at` (our
  clock) and a monotonic local sequence number.
* Latency is measured as `received_at − source_time`, which conflates broker
  clock skew with transport delay. It is labelled `broker_clock_basis` and is
  never presented as exchange latency.
* Out-of-order ticks are rejected and counted (never quietly reordered into a
  decision); duplicates are rejected by content identity *and* sequence.
* No figure is ever presented as "global market" data: one broker feed is one
  broker feed, and the text says so wherever flow is discussed.

## Safety

* Paper only, enforced in configuration and in the venue constructor, not in the UI.
* AX-GUARDIAN holds frozen limits; no method mutates them and no strategy object
  is given a reference that could.
* Entry suspension (emergency stop) is separate from position management: an
  emergency stop stops *new* entries and leaves the defined protection procedure
  (broker-side stops, management loop) in force.
* Unknown submission outcomes are recorded and reconciled; they are never retried
  blindly.

## Research honesty

No parameter is chosen because a backtest looked good. Thresholds are declared in
code before the lab is run, the lab's own numbers are reported with sample sizes
and confidence intervals, and a strategy is eligible for the desk only with a
SKEPTIC approval artifact whose parameter fingerprint matches the code that is
running.

## Lifecycle of one tick

```
on_tick(symbol, bid, ask, source_time, received_at, volume, source)
  │
  ├─ TickCore.ingest ──────────── refused? count it, write the refusal, return
  │                               accepted? drain new quality events to the ledger
  ├─ PositionBook.on_tick ─────── stop / target / time / stale / flip / break-even /
  │                               trail, per open position on this symbol
  │                                 └─ closed? cost it, settle it, record net + won
  ├─ entry gates ──────────────── resyncing · stale · warming up · unresolved order ·
  │                               guardian suspension · cooldown · position cap
  ├─ regime.classify ──────────── no-trade label? write it, stop here
  ├─ per strategy ─────────────── opportunity → signal → probability → EV → size →
  │                               guardian → precheck → submit once → verify stop
  └─ ledger ───────────────────── every branch above writes its own entry
```

Management runs **before** any entry gate, and it is never skipped because entries are
suspended. That ordering is the safety property, not an implementation detail.

## Public API

`from axiom_forex.scalper import ...` exports the whole desk: the configuration types
(`ScalperConfig`, `InstrumentSpec`, `CostTerms`, `fixture_config`), the pipeline
(`TickCore`, `classify`, `RegimeState`, the five strategy classes and their parameter
dataclasses, `OutcomeBook`, `evaluate`, `size_position`), the controls (`Guardian`,
`GuardianLimits`, `Proposal`, `Trader`, `PaperVenue`, `PositionBook`), the evidence
tools (`Scribe`, `verify_jsonl`, `skeptic.validate`, `Criteria`, `ValidationReport`,
`ApprovalRegistry`) and the lab (`run_scenario`, `collect_outcomes`, `repriced`,
`degradation_check`, `write_report`). Module-level constants that a caller must compare
against (`TRENDING_UP`, `LOW_LIQUIDITY`, `ABNORMAL`, `BUY`, `SELL`, `SIGNAL`,
`NO_SETUP`, …) are exported too, so a caller never has to reach into a submodule.

`ScalperDesk` is the only stateful object. It is constructed from a validated
`ScalperConfig`; it accepts an injected venue, guardian, scribe and probability source so
that the same desk can run against a simulation, a future adapter, or a shared
account-level guardian used by the bar engine.

## Scripts

| Script | What it does |
| --- | --- |
| `scripts/scalper_lab.py` | Trains on a declared training replay, freezes the posteriors, evaluates on a time-shifted replay, runs the skeptic, and writes `docs/SCALPER_LAB_REPORT.md` — including the cost-stress "forward" comparison and the verified/unverified split. |
| `scripts/scalper_paper.py` | Runs one paper session over synthetic ticks, a tick file, or the lab's training replay; seals its ledger and re-verifies the file from disk. |

Both are pure: no network, no terminal, no order path.

## Document set

| Document | Contents |
| --- | --- |
| `docs/SCALPER_ARCHITECTURE.md` | this file: module map, boundaries, lifecycle, API |
| `docs/SCALPER_CONFIGURATION.md` | every declared field, its default, and how a bad value is refused |
| `docs/SCALPER_STRATEGIES.md` | the five rule families, the eligibility matrix, and why order flow is not admitted |
| `docs/SCALPER_RISK_CONTROLS.md` | EV, variance, guardian, the emergency-stop procedure, and why limits are not agent-configurable |
| `docs/SCALPER_EXECUTION_AND_RECOVERY.md` | submission sequence, protection, unknown outcomes, reconciliation, restart |
| `docs/SCALPER_PAPER_RUNBOOK.md` | how to run a paper session, how to stop safely, and the known limitations |
| `docs/SCALPER_LAB_REPORT.md` | machine-generated measurements on synthetic data, with the refusal |

## Verification status

Every behaviour listed in this document is covered by tests under `tests/test_scalper_*`,
and the suite is run before a commit. What the suite can prove is that the machinery
behaves as described **against fakes and replay**. It cannot prove anything about a
MetaTrader 5 terminal, a broker's costs, or a live market, and the report and runbook say
so rather than leaving a reader to assume otherwise.
