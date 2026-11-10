# AX-SCALPER signed evidence and admission policy

This document describes the local evidence gate used by the MT5-live-quote **PAPER** observation host. It does not claim profitability, does not enable broker orders, and is not a LIVE approval. Without a complete policy and an eligible artifact for an exact strategy fingerprint, the desk runs observation-only (or does not start if connection, configuration, or recovery checks fail); it generates no strategy trades.

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

An artifact uses schema `axiom-scalper-evidence-v1` and is named `<exact fingerprint>.json`. It binds, in a canonical UTF-8 JSON representation (sorted keys, compact separators, no NaN values), every field other than `signature` to an Ed25519 signature. `signature` is the 64-byte signature encoded as hexadecimal. The artifact must include:

- `reviewer_id`, `strategy_developers`, and exact `strategy` identity (`strategy_id`, `rule_version`, `fingerprint`);
- the exact connected broker instrument `audit_view`, timeframe and explicit holding/exit rule;
- `evaluation_method: "chronological_holdout_walk_forward_v1"`;
- broker data provenance (`MT5_BROKER_HISTORICAL_EXPORT`, `MT5_BROKER_TICK_EXPORT`, or `MT5_BROKER_LIVE_CAPTURE` only), source, timezone-aware range, relative filename and SHA-256 of the source file;
- `costs` in account currency, including commission, spread, slippage, verified flag, and observed commission/slippage states; commission/slippage must exactly match the current PAPER configuration and be `OBSERVED`;
- chronological `trades` rows in `train`, `validation`, then `test` splits, with timezone-aware timestamp, gross/cost/net account amounts and regime;
- walk-forward fold test returns, at least two parameter variants including the exact candidate, peer-symbol aligned return series, overlap intervals with exposure fractions, signed multiple-testing results (`multiple_testing.pbo`, `multiple_testing.deflated_sharpe`, `multiple_testing.q_value`), and a replay manifest (`replay_version`, `engine_digest`, `input_digest`, `output_digest`). The manifest is not itself proof of reproduction; the production host currently has no raw replay verifier and rejects it.

The gate verifies the signature/trust, source-file digest and path, exact strategy/instrument/cost identity, chronology and split boundaries, arithmetic `gross - cost = net`, sample counts, deterministic bootstrap lower bound on mean OOS net expectancy, Wilson win-rate lower bound, PBO/deflated-Sharpe/q-value against the declared policy, test-set profit factor (recomputed from net returns), maximum drawdown, 95% CVaR, stress costs, each fold and regime, parameter sensitivity, peer correlation and overlap against the declared policy. It does not interpret empty/missing analysis as zero risk. A signed but invalid artifact is refused, and there is no automatic fallback to sample data.

**Important evidence limitation:** the workstation recomputes the listed trade-result statistics but does not yet have a production raw-tick replay verifier wired into the admission host. `evaluate_artifact` therefore fails closed with `evidence_raw_replay_verifier_unavailable` when no verifier is supplied; a signature and reviewer assertion alone cannot open admission. A callback contract and digest-match checks exist for focused tests, but no production implementation currently parses the referenced dataset, rebuilds the exact strategy/configuration, and reproduces signals/fills. Do not treat an artifact as independently reproduced merely because its signature verifies. Automated raw-data replay/reproduction remains an implementation blocker; no candidate can pass the production host's independent-replay gate in this build.

## PAPER and LIVE boundaries

`Connect` is an explicit observation-session action. AX-SCALPER reads live quotes and simulates PAPER fills locally; it does not call order-send, order-check, close, account-mutation, or live execution APIs. If no candidate qualifies, no strategy is enabled; Start (when ordinary connection/configuration/recovery checks pass) is observation-only and its PAPER desk has no strategies. PAPER outcomes are not fed back into admitted OOS evidence.

No AX-SCALPER order is automatically handed to LIVE. A PAPER result never arms or authorizes LIVE. Any later LIVE request must be independently selected, confirmed, armed, preflighted, risk checked, idempotently submitted, reconciled, and protected through AXIOM's existing guarded `/live` workflow. This host has no LIVE adapter or handoff implementation.

Any prior scalper ledger remains blocked on a subsequent start in this build, including an integrity-valid flat run: desk, TickCore, guardian and strategy history are not restored exactly. The only available recovery action records the explicitly typed `KEEP_PAPER_BOOK_BLOCKED` decision and leaves positions and ledgers untouched. It does not resume, flatten, clear, or unlock the book.
