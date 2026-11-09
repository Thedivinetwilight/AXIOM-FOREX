# AX-SCALPER Autopilot integration: migration and safety plan

## Current architecture and data path

The workstation's explicit Connect action owns an `ObservationSession`; its
`market_data_source()` returns the already-connected read-only `MT5Terminal`, and
`MT5Terminal.tick(symbol)` reads validated bid/ask/time. The guarded LIVE adapter
is held separately by the existing LIVE workflow. The scalper `MT5LiveQuoteSource`
uses only the observation session; `LivePaperPump` passes quotes to `ScalperDesk`
and `PaperVenue`. The current pump is an API only: launch does not build it and
Autopilot has no actions for it.

Existing paper starting capital is written/read by the paper-account subsystem
under `paper-capital.local.json`; it is not the MT5 balance. Existing terminal
symbol records are obtained through the connected observation session. Broker
execution/commission facts and account conversion currency are not sufficient by
themselves to infer every AX-SCALPER cost/risk input. Existing provider setup has
operator-entered transaction cost and risk terms; it may be reused only after
explicit validation and identity matching. Fixture scalper defaults are never
valid live-runner configuration.

## Configuration inputs and policy

A runner may be constructed only after explicit Connect and only from a validated
configuration bound to the exact broker symbol and exact strategy/version set.
The terminal supplies broker symbol point/digits/contract/volume/stop facts. The
existing paper-capital document supplies local PAPER capital. Operator-declared
cost terms and risk limits must be explicit and in named units; unsupported or
missing terms block start. No terminal balance, fixture term or package default
may fill a missing value. LIVE mode is rejected at both config and venue.

## Recovery state contract

Exact recovery needs schema/version, config and strategy fingerprints, symbol,
side, size, entry fill, quote provenance, stop/target/exit policy, order outcome,
position status, risk/guardian state, realized equity and idempotency keys for the
append-only event sequence. A restart may resume only if the complete, hash-verified
state matches current config and strategy identities and every open position's
protection/management state can be reconstructed. This increment will not fabricate
positions from partial records: unreadable, incompatible or incomplete state means
`RECOVERY_REQUIRED`, no new entries, no automatic flatten, and the stored paper
book remains available for operator review. Position management must not resume
without exact reconstruction.

## Increment boundary

The page integration uses nonce-protected explicit Start/Stop actions and the
already-connected observation session. The page reports the validated config,
source, quote freshness, decisions, simulated fills and recovery state. If no
complete live configuration or exact recovery state exists, Start is refused with
a named remediation. Existing bar Autopilot and LIVE actions are not modified.
Any unimplemented input remains a blocker; this plan does not authorize substituting
fixture values or weakening guards.
