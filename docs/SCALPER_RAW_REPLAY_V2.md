# AX-SCALPER independent raw replay — evidence v2

This is a replay-verifier vertical slice, not an approved strategy or a trading-readiness claim. A reviewer signature authenticates the submitted claims and their provenance; the host independently recomputes results from the raw artifact. Missing or mismatched evidence remains in the evidence directory and yields a named NO-TRADE blocker. The replay runs only in memory and uses AX-LAB's strategy, TickCore, `PaperVenue`, costs, and position management. It does not open the active PAPER ledger, consume host outcomes, or persist replay trades.

## Supported artifact (v2 slice)

The signed JSON document uses the versioned `axiom-scalper-evidence-v2` outer schema and requires these production replay fields:

- `data`: `origin` (`MT5_BROKER_HISTORICAL_EXPORT` or `MT5_BROKER_TICK_EXPORT`), source/provenance, internal `symbol`, exact `broker_symbol`, timezone-qualified inclusive `start`/`end`, sibling `file`, and its SHA-256 `sha256`.
- Raw sibling file: UTF-8 CSV with header `timestamp,symbol,broker_symbol,bid,ask,split`. Timestamps must be timezone-qualified and strictly increasing; quotes finite, positive, and `ask > bid`; all rows must match both declared symbol identities and lie in the declared date range. Every split must contain rows and appear chronologically as `train`, `validation`, `test`. Other layouts are unsupported, not guessed.
- `strategy`: exact `strategy_id`, `rule_version`, and strategy fingerprint matching a built-in AX-SCALPER strategy.
- `replay_config`: `configuration_digest` is the SHA-256 of the canonical JSON of the **complete** local PAPER configuration, taken as a dataclass dump (`dataclasses.asdict(ScalperConfig)`) — not the shorter `ScalperConfig.audit_view()` projection, which omits fields such as `tick_window`, `cooldown_ms` and the trailing-stop terms. Canonical JSON here means `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`, `allow_nan=False` and `default=str`, encoded UTF-8; a `Decimal` therefore appears as its decimal string. `strategy_source_digest` binds the replay engine's own source files, in the fixed order listed by `raw_replay.source_digest()`; because it is a digest of code, any edit to those files invalidates existing artifacts.
- `instrument`: exact broker instrument audit view. `costs`: exact local declared cost terms plus `verified: true`; local matching and existing observed-cost policy are still required.
- `walk_forward`: non-empty list of `{train_start, train_end, test_start, test_end}` timezone-qualified boundaries. Each train interval must precede its test interval; windows are ordered and non-overlapping, and both intervals must contain raw ticks.
- `replay`: `replay_version: axiom_scalper_raw_replay_v1`, `engine_digest`, `input_digest`, `output_digest`, and `claims`. `claims` is the canonical AX-LAB audit output for validation, test, and each walk-forward window — that is `{validation, test, walk_forward: [{window, result}]}`, where each `result` is the run's audit view with its `ledger` entry removed and its closed `trades` added. The input digest binds raw file digest and normalized rows, strategy, full configuration, instrument, costs, and windows. Output digest binds recomputed signal/fill funnel, cost-aware closed trades, and metrics.

## When the verifier runs

`verify_raw_artifact` is not a standalone step an operator triggers. `ScalperPaperHost._strategy_review` (in `axiom_forex/scalper/live_host.py`) reads the evidence directory while building its per-candidate review, and passes the verifier to `axiom_forex/scalper/admission.py::evaluate_artifact` only when it has already resolved the exact live-PAPER configuration — a connected terminal, a verified instrument view and observed cost terms via `load_live_paper_config`.

So the same artifact can be refused for a reason that has nothing to do with its contents. The checks are sequential and the first failure names the reason:

| State of the host | What the artifact gets |
| --- | --- |
| no `scalper_admission.local.json`, or a policy that fails validation | `admission_policy_missing: …` |
| terminal not connected, or instrument / cost terms unresolved | the host's own missing-terms reason; the verifier is never called |
| connected, but the artifact has no complete `replay` block | `evidence_replay_manifest_missing` |
| connected, configuration resolved, artifact complete | the replay runs; any failure is named (`raw_tick_chronology_invalid:line=…`, `evidence_data_digest_mismatch`, `evidence_replay_claims_mismatch:claims.…`, `evidence_replay_output_digest_mismatch`, …) |
| everything above passes, then the signed policy limits are compared | a policy reason, or `eligible_under_signed_policy_and_reproduced_replay` |

A pass is therefore the conjunction of a signature, a reproducible replay, and the declared policy limits — and admission still compares the replayed test trades with the artifact's own `test` rows one by one (count, close timestamp in UTC, regime, gross, cost and net within `0.000001`) whenever `replay_version` is `axiom_scalper_raw_replay_v1`.

The verifier accepts only the two broker-export origins, `MT5_BROKER_HISTORICAL_EXPORT` and `MT5_BROKER_TICK_EXPORT`. `MT5_BROKER_LIVE_CAPTURE` satisfies the earlier policy origin check but is refused here with `evidence_data_origin_not_independent`, so it cannot reach eligibility. It also refuses any `replay_version` other than `axiom_scalper_raw_replay_v1` (`replay_version_unsupported`); the outer schema is likewise fixed at `axiom-scalper-evidence-v2`.

The verifier builds calibration only from each applicable train interval using AX-LAB's measured bracket outcomes; it then runs validation/test and every declared walk-forward window through the actual strategy and PAPER fill implementation. Any configuration/source/data/claim drift fails closed. Existing admission also evaluates the signed policy metrics and corroborating claims. Passing raw replay is necessary, not sufficient.

## Operator workflow

1. Keep the workstation and its data separate. Obtain a broker-origin raw tick export through the documented maintenance/import process. Do not create, edit, resample, reorder, or synthesize ticks to fit the format.
2. Preserve the broker export unchanged beside the signed evidence JSON. Record exact symbol, broker symbol, UTC range, and the chronological partition boundaries. Keep costs and instrument facts sourced from observed broker/configuration data; do not infer missing terms.
3. Have an independent reviewer build/sign the complete evidence document under the repository's existing canonical Ed25519 signing procedure and verify the input, engine, and output digests produced by the same versioned replay specification. A signature alone is never sufficient.
4. Place the pair in the configured `scalper_evidence/` directory under the candidate fingerprint filename. Do not replace or delete failed artifacts. Inspect Autopilot's candidate reason and replay audit result. A blocker means no candidate admission and no entries.
5. Only a separately verified real broker artifact can demonstrate that the format and replay are suitable in practice. The deterministic repository fixtures are tests, not evidence and cannot qualify a candidate.

Two neighbouring pieces are separate from this verifier and must not be read into it. First, durable PAPER restoration: a strategy-free observation ledger can be resumed only after its own exact input/event replay, while strategy-bearing runs, open positions and any ledger without that manifest stay blocked (see [SCALPER_ADMISSION.md](SCALPER_ADMISSION.md) and [AXSCALPER_WINDOWS_MAINTENANCE.md](AXSCALPER_WINDOWS_MAINTENANCE.md)). Second, LIVE: there is no automatic PAPER-to-LIVE handoff by design, and the guarded one-shot bridge on `/live` is the only route — it consumes an eligible admission result as one input and still requires its own signed exact-plan approval, account confirmation, arming and preflight ([SCALPER_LIVE_BRIDGE.md](SCALPER_LIVE_BRIDGE.md)). No Windows or real-MT5 verification is implied by these fixtures, and none of them is an approved strategy.
