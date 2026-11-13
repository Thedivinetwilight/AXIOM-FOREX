# AX-SCALPER signed evidence and admission policy

This document describes the local evidence gate used by the MT5-live-quote **PAPER** observation host. It does not claim profitability, does not enable broker orders, and is not a LIVE approval. Without a complete policy and an eligible artifact for an exact strategy fingerprint, the desk runs observation-only (or does not start if connection, configuration, or recovery checks fail); it generates no strategy trades. The artifact's raw-replay half — the file format, digest bindings and replay behaviour — is specified in [SCALPER_RAW_REPLAY_V2.md](SCALPER_RAW_REPLAY_V2.md).

## Local files and preservation

The workstation derives these paths from the parent directory of `scalper_paper.local.json`:

- `scalper_admission.local.json` — operator-declared typed policy and trusted reviewer public keys.
- `scalper_evidence/<strategy-fingerprint>.json` — a signed review for the exact strategy fingerprint.
- Any evidence data file named by the artifact, resolved beneath that evidence directory.

These files are operator-managed local state. Back them up before application updates; do not put broker exports, keys, policy, artifacts, ledgers, or settings in Git. Reviewer **private** keys must remain with reviewers and must never be copied into the workstation. Only the Ed25519 public key is stored in the local policy.

## Policy schema

Create the policy file with schema `axiom-scalper-admission-policy-v1`. Every value is required; blank placeholders intentionally fail closed. The software supplies no production thresholds. The policy currency must equal the locally configured PAPER capital currency.

```json
{
  "schema": "axiom-scalper-admission-policy-v1",
  "currency": "",
  "min_total_trades": "",
  "min_oos_trades": "",
  "min_walk_forward_folds": "",
  "minimum_net_expectancy": "",
  "minimum_win_rate_lcb_95": "",
  "maximum_pbo": "",
  "minimum_deflated_sharpe": "",
  "minimum_profit_factor": "",
  "maximum_q_value": "",
  "maximum_drawdown": "",
  "maximum_tail_loss_cvar_95": "",
  "cost_stress_multiple": "",
  "maximum_parameter_sensitivity_fraction": "",
  "maximum_absolute_correlation": "",
  "maximum_overlapping_exposure_fraction": "",
  "maximum_artifact_age_days": "",
  "trusted_reviewers": {}
}
```

An authorized policy owner must select the currency and every numerical limit from the intended mandate, capital, broker/instrument facts, and risk authority. No value in this example is a recommendation. Integer sample/fold/age limits must be positive; net expectancy, drawdown, and tail-loss limits must be positive account-currency amounts; the win-rate lower bound, PBO ceiling, deflated-Sharpe floor, q-value ceiling, sensitivity, correlation, and overlap values are fractions in `[0, 1]`; profit factor is positive; cost stress is at least `1`. Unknown fields and invalid or missing values are rejected. The UI exposes the policy path, effective declared values, evidence location, per-candidate decision and key metrics; editing the local file is a separate operator action.

`trusted_reviewers` maps a reviewer identifier to a base64 Ed25519 public key (32 raw bytes). The reviewer identifier must not occur in `strategy_developers` in the artifact. Trust must be established out of band by the policy owner; a key pasted into this file is not independently authenticated by the application.

## Evidence artifact contract

An artifact uses the outer schema `axiom-scalper-evidence-v2` and is named `<exact fingerprint>.json`. A document declaring `axiom-scalper-evidence-v1` is refused with `evidence_schema_invalid`; there is no compatibility path. It binds, in a canonical UTF-8 JSON representation (sorted keys, compact separators, no NaN values, `default=str` for values JSON cannot carry — a `Decimal` becomes its string), every field other than `signature` to an Ed25519 signature. `signature` is the 64-byte signature encoded as hexadecimal. The artifact must include:

- `reviewer_id`, `strategy_developers`, and the exact `strategy` identity (`strategy_id`, `rule_version`, `fingerprint`);
- the exact connected broker `instrument` audit view, and a `horizon` carrying a non-empty `timeframe` and `holding_rule`;
- `evaluation_method: "chronological_holdout_walk_forward_v1"`;
- broker `data` provenance (`origin`, `source`, timezone-aware inclusive `start`/`end`, the sibling `file`, and its SHA-256 `sha256`);
- `costs` in the policy currency with `verified: true`, `commission_state` and `slippage_state` equal to `OBSERVED`, and `currency`, `commission_per_lot` and `slippage_points` exactly matching the current PAPER configuration;
- chronological `trades` rows in `train`, `validation`, then `test` splits, each with a timezone-aware `timestamp`, gross/cost/net account amounts and a `regime`;
- `walk_forward` folds carrying their own `test_net_account` returns, at least two `parameter_sensitivity` variants including the exact candidate, peer `portfolio.returns` series, `portfolio.overlap_intervals` with exposure fractions, and signed `multiple_testing` results (`pbo`, `deflated_sharpe`, `q_value`);
- `replay_config` (the configuration and strategy-source digests) and a `replay` block: `replay_version: "axiom_scalper_raw_replay_v1"`, `engine_digest`, `input_digest`, `output_digest` and `claims`.

The `replay` block is not itself proof of anything. It is compared against a replay of the named raw file, and the raw file format, the digest bindings and the claim shape are specified in [SCALPER_RAW_REPLAY_V2.md](SCALPER_RAW_REPLAY_V2.md).

The gate verifies the signature/trust, source-file digest and path, exact strategy/instrument/cost identity, chronology and split boundaries, arithmetic `gross - cost = net`, sample counts, deterministic bootstrap lower bound on mean OOS net expectancy, Wilson win-rate lower bound, PBO/deflated-Sharpe/q-value against the declared policy, test-set profit factor (recomputed from net returns), maximum drawdown, 95% CVaR, stress costs, each fold and regime, parameter sensitivity, peer correlation and overlap against the declared policy. It does not interpret empty/missing analysis as zero risk. A signed but invalid artifact is refused, and there is no automatic fallback to sample data. `data.origin` may name `MT5_BROKER_HISTORICAL_EXPORT`, `MT5_BROKER_TICK_EXPORT` or `MT5_BROKER_LIVE_CAPTURE` at this policy layer, but the raw replay described below accepts only the two export origins — a live-capture artifact passes this check and is then refused by the replay with `evidence_data_origin_not_independent`.

## Independent raw replay: when it runs, what it re-computes, and what it cannot prove

**Where.** The verifier is `axiom_forex/scalper/raw_replay.py` (`verify_raw_artifact`). `ScalperPaperHost._strategy_review` in `axiom_forex/scalper/live_host.py` passes it to `evaluate_artifact` as `replay_verifier`.

**When it is supplied.** Only once that host has resolved the exact live-PAPER configuration: a connected terminal, a verified instrument view, and observed cost terms (`load_live_paper_config`). In every other case — terminal disconnected, configuration unresolvable, instrument or cost terms missing — no verifier is supplied, and an otherwise complete artifact is refused with `evidence_raw_replay_verifier_unavailable`, or with an earlier and more specific refusal such as `admission_policy_missing` or the missing-instrument reason. The `replay` block is required before the verifier is consulted at all: an artifact missing `replay_version`, `engine_digest`, `input_digest` or `output_digest` is refused with `evidence_replay_manifest_missing`, whether or not a verifier is present.

**What the replay checks.** It reads the named sibling file and refuses anything but a UTF-8 CSV with the documented header, timezone-qualified strictly increasing ticks inside the declared range, matching symbol and broker symbol on every row, finite positive quotes with `ask > bid`, and `train`/`validation`/`test` rows present in that chronological order. It checks the file's SHA-256, that the path is a regular file inside the evidence directory, the exact strategy identity, the configuration digest, the replay-engine source digest, the exact `instrument` audit view, and `costs` down to `verified: true`. It then re-runs calibration from the `train` interval, drives `validation`, `test` and every declared walk-forward window through AX-LAB's actual strategy, TickCore and `PaperVenue` fills, recomputes the input digest, the output digest and the signed `claims` block, and refuses any mismatch by name (`evidence_replay_claims_mismatch:<path>`, `evidence_replay_output_digest_mismatch`, and so on). For `replay_version: "axiom_scalper_raw_replay_v1"` admission additionally compares the replayed test trades with the artifact's own `test` rows — count, close timestamp, regime, gross, cost and net within `0.000001` — and refuses `evidence_replayed_test_trade_count_mismatch` or `evidence_replayed_test_trade_mismatch`.

**What a pass does not prove.** Reproducibility by this engine version with this configuration. Nothing more. It is not a profitability claim, not a LIVE approval, and not evidence about any real market:

- **No genuine artifact has been through it here.** The verifier is exercised only by the deterministic fixtures in `tests/test_scalper_raw_replay.py`. This repository contains no policy, no evidence artifact and no approval, and no fixture may be treated as evidence.
- **Provenance is the reviewer's, not the application's.** The signature and the out-of-band trusted key assert where the CSV came from; the application verifies the file it was handed, not the broker that produced it.
- **The engine digest is part of the identity.** `strategy_source_digest` hashes the replay engine's own source files, so any change to them makes an existing artifact fail, and the artifact must be re-signed and re-verified against the engine that produced it.
- **A pass is one prerequisite.** It opens nothing by itself: the separate signed exact-plan approval, account confirmation, arming, preflight and one-shot LIVE bridge are described in [SCALPER_LIVE_BRIDGE.md](SCALPER_LIVE_BRIDGE.md), and no candidate in this build has any of them.
- **Windows and a real terminal remain unverified.** Every statement here about the host's readiness is about code paths exercised with fixtures.

## PAPER and LIVE boundaries

`Connect` is an explicit observation-session action. AX-SCALPER reads live quotes and simulates PAPER fills locally; it does not call order-send, order-check, close, account-mutation, or live execution APIs. If no candidate qualifies, no strategy is enabled; Start (when ordinary connection/configuration/recovery checks pass) is observation-only and its PAPER desk has no strategies. PAPER outcomes are not fed back into admitted OOS evidence.

No AX-SCALPER order is automatically handed to LIVE. A PAPER result never arms or authorizes LIVE. This host holds no live adapter, exposes no order-submitting API, and never pushes a signal outward. Any later LIVE request must be independently selected, confirmed, armed, preflighted, risk checked, idempotently submitted, reconciled, and protected through AXIOM's existing guarded `/live` workflow — the one-shot bridge in [SCALPER_LIVE_BRIDGE.md](SCALPER_LIVE_BRIDGE.md), which reads this host's per-candidate eligibility as one input and still requires its own signed exact-plan approval.

A prior scalper ledger blocks a subsequent start unless it is a **strategy-free observation run** that carries the versioned replay manifest: that one case is resumed only after every stored quote and event is replayed and compared, and a single mismatch leaves it blocked. Strategy-bearing runs, ledgers with open positions or unresolved paper orders, summary-only ledgers, integrity failures and any replay mismatch all stay blocked — desk, TickCore, guardian and strategy history are not restored exactly, and no replacement run may silently reset them. The other available recovery action records the explicitly typed `KEEP_PAPER_BOOK_BLOCKED` decision and leaves positions and ledgers untouched; it does not resume, flatten, clear, or unlock the book.
