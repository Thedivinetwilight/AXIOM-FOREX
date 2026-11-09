# AX-SCALPER rapid-trading integration audit (current branch)

## Runtime paths and safety boundaries

- **Explicit quotes:** the `/setup` Connect action creates the workstation's
  observation-only MT5 session. `MT5LiveQuoteSource` accepts reads only from that
  connected session, verifies exact symbol identity, bid/ask and source timestamp,
  applies freshness checks, and does not call connect/reconnect itself.
- **Normalization and quality:** `LivePaperPump` supplies quotes to
  `ScalperDesk.on_tick`; `TickCore` validates, sequences, deduplicates and records
  clock/gap/spread quality. Status is per selected broker symbol. This is a quote
  poller, not a broker-wide order-book feed.
- **Features and signals:** accepted ticks build the bounded windows used by
  `regime.classify` and the deterministic strategy family implementations in
  `signals.py`. Strategy evaluations and refusals are ledgered. Order-flow strategies
  remain ineligible if their data requirement is unavailable.
- **Admission, EV and risk:** `ScalperDesk` obtains probability evidence, evaluates
  net EV against declared costs, sizes using verified instrument terms and local
  paper equity, and asks `Guardian` before submission. A probability estimate with
  fewer than `DeskThresholds.min_probability_samples` is now a hard refusal with a
  reason in the ledger; it cannot leak into EV as a weak prior. The live-quote host
  deliberately supplies no independent evidence, and does not self-train on its own
  paper outcomes. Therefore the current production candidate set has zero admitted
  strategies and the correct behavior is no trade.
- **Evidence threshold:** the review UI surfaces the existing `skeptic.Criteria`
  policy: 200 total trades, 60 held-out trades, 3 usable walk-forward folds,
  survival at 1.5x modeled costs, PBO at most 0.25, deflated Sharpe at least 0.95,
  profit factor at least 1.10, and a positive confidence interval. These are project
  policy values, not newly inferred performance claims. Validation additionally
  covers chronological split/OOS expectancy, multiple testing, uncertainty,
  regime/session breakdown and cost stress. The current policy does **not** define
  maximum drawdown, tail-loss or portfolio-correlation cutoffs; those are explicitly
  surfaced as undecided policy inputs, not guessed. Until authorized limits and an
  independent evidence artifact exist, no candidate can qualify. The synthetic lab
  report is not broker-symbol live evidence and does not approve any current candidate.
- **PAPER execution:** only `PaperVenue` is passed to the desk. It creates local
  fills/positions and the pump source has no MT5 mutation/execution adapter. The host
  writes append-only JSONL plus an atomic head sidecar, and status reports accepted
  ticks, quote freshness, decision/refusal, simulated counts, positions and P&L only
  when the necessary account-currency point value and quote are available.
- **Restart/recovery:** the checkpoint validates the chain head/count and catches
  trailing truncation. Recovery scanning identifies open positions, unknown fills,
  missing entry links, duplicate opens, invalid event schema, wrong mode and missing
  fields needed to restore identity. Exact guardian/strategy/position-manager replay
  is not implemented. Any unresolved or integrity-failed ledger blocks Start; the
  acknowledgement records review only and cannot clear the block, resume, flatten,
  or edit the source ledger. Preserve the JSONL and sidecar unchanged. Exact recovery
  tooling remains open.
- **LIVE:** AX-SCALPER does not own or import an MT5 execution adapter. No current
  candidate is eligible, so it has no LIVE intent to route. The separate existing
  `/live` workflow remains the only LIVE path and continues to own mode confirmation,
  account binding, arming, preflight, risk, idempotency, unknown-outcome handling,
  reconciliation and protective-order checks. Do not treat a PAPER signal as a LIVE
  intent; a future adapter must explicitly translate an approved intent into that
  existing path and pass its normal gate.

## Current operator capability and limits

The Autopilot card exposes a nonce-protected symbol picker populated only from exact
verified tradable terminal Forex symbols. Saving changes only the local scalper
settings' selected `symbol`, leaving other local cost/risk terms intact. Missing
terms continue to block Start. The operator must first Connect explicitly on Setup.

Start currently runs PAPER quote monitoring and decision auditing, **not a profitable
or live-ready strategy**. Because there is no independent broker-symbol OOS approval
artifact, orders and fills remain zero. Exact state restoration, a signed/reviewed
strategy evidence artifact loader, live-symbol multi-instrument portfolio routing,
and a PAPER-to-existing-LIVE intent bridge are not implemented. No aspirational
return, win-rate claim, or profitability claim is made.

## Verification scope

Fake-based tests cover quote/session isolation, symbol validation, PAPER ledger and
checkpoint integrity, restart blockers, nonce-protected controls, risk/config
refusals, no-trade on thin probability evidence, P&L availability, and guards around
LIVE routing. They do not verify Windows MT5 installation, real terminal quote
semantics, broker specifications, costs, account state, or any live order.
