# AX-SCALPER independent raw replay — evidence v2

This is a replay-verifier vertical slice, not an approved strategy or a trading-readiness claim. A reviewer signature authenticates the submitted claims and their provenance; the host independently recomputes results from the raw artifact. Missing or mismatched evidence remains in the evidence directory and yields a named NO-TRADE blocker. The replay runs only in memory and uses AX-LAB's strategy, TickCore, `PaperVenue`, costs, and position management. It does not open the active PAPER ledger, consume host outcomes, or persist replay trades.

## Supported artifact (v2 slice)

The signed JSON document uses the versioned `axiom-scalper-evidence-v2` outer schema and requires these production replay fields:

- `data`: `origin` (`MT5_BROKER_HISTORICAL_EXPORT` or `MT5_BROKER_TICK_EXPORT`), source/provenance, internal `symbol`, exact `broker_symbol`, timezone-qualified inclusive `start`/`end`, sibling `file`, and its SHA-256 `sha256`.
- Raw sibling file: UTF-8 CSV with header `timestamp,symbol,broker_symbol,bid,ask,split`. Timestamps must be timezone-qualified and strictly increasing; quotes finite, positive, and `ask > bid`; all rows must match both declared symbol identities and lie in the declared date range. Every split must contain rows and appear chronologically as `train`, `validation`, `test`. Other layouts are unsupported, not guessed.
- `strategy`: exact `strategy_id`, `rule_version`, and strategy fingerprint matching a built-in AX-SCALPER strategy.
- `replay_config`: `configuration_digest` is SHA-256 of canonical JSON from the exact local PAPER `ScalperConfig.audit_view()`; `strategy_source_digest` binds the replay engine source files.
- `instrument`: exact broker instrument audit view. `costs`: exact local declared cost terms plus `verified: true`; local matching and existing observed-cost policy are still required.
- `walk_forward`: non-empty list of `{train_start, train_end, test_start, test_end}` timezone-qualified boundaries. Each train interval must precede its test interval; windows are ordered and non-overlapping, and both intervals must contain raw ticks.
- `replay`: `replay_version: axiom_scalper_raw_replay_v1`, `engine_digest`, `input_digest`, `output_digest`, and `claims`. `claims` is the canonical AX-LAB audit output for validation, test, and each walk-forward window. The input digest binds raw file digest and normalized rows, strategy, full configuration, instrument, costs, and windows. Output digest binds recomputed signal/fill funnel, cost-aware closed trades, and metrics.

The verifier builds calibration only from each applicable train interval using AX-LAB's measured bracket outcomes; it then runs validation/test and every declared walk-forward window through the actual strategy and PAPER fill implementation. Any configuration/source/data/claim drift fails closed. Existing admission also evaluates the signed policy metrics and corroborating claims. Passing raw replay is necessary, not sufficient.

## Operator workflow

1. Keep the workstation and its data separate. Obtain a broker-origin raw tick export through the documented maintenance/import process. Do not create, edit, resample, reorder, or synthesize ticks to fit the format.
2. Preserve the broker export unchanged beside the signed evidence JSON. Record exact symbol, broker symbol, UTC range, and the chronological partition boundaries. Keep costs and instrument facts sourced from observed broker/configuration data; do not infer missing terms.
3. Have an independent reviewer build/sign the complete evidence document under the repository's existing canonical Ed25519 signing procedure and verify the input, engine, and output digests produced by the same versioned replay specification. A signature alone is never sufficient.
4. Place the pair in the configured `scalper_evidence/` directory under the candidate fingerprint filename. Do not replace or delete failed artifacts. Inspect Autopilot's candidate reason and replay audit result. A blocker means no candidate admission and no entries.
5. Only a separately verified real broker artifact can demonstrate that the format and replay are suitable in practice. The deterministic repository fixtures are tests, not evidence and cannot qualify a candidate.

Exact durable PAPER state restoration and the guarded AX-SCALPER LIVE-intent handoff remain separate unimplemented blockers. No Windows or real-MT5 verification is implied by these tests.
