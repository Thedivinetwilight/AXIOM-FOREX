# Guarded AX-SCALPER one-shot LIVE bridge

This document describes the **only** AX-SCALPER route to a real MetaTrader 5
order. It is deliberately separate from the PAPER command centre on
`/autopilot`, and it does not promote a PAPER result. A real deployment has no
admitted evidence artifact and no signed exact-plan approval by default, so the
LIVE bridge is **unavailable until both exist**. The `v3` typed-intent contract
is a message format, not execution authority.

> Nothing in this repository is evidence that a strategy makes money. All tests
> use synthetic keys, synthetic evidence and a fake MT5 terminal. No genuine
> AX-SCALPER candidate has passed independent raw-replay admission here.

## PAPER and LIVE stay separate

| Surface | What it can do | What it cannot do |
| --- | --- | --- |
| `/autopilot` AX-SCALPER PAPER | Read live terminal quotes, run a local simulated book, verify signed raw-replay evidence, report admission decisions | Connect a live adapter, approve LIVE, arm LIVE, send or close a broker order |
| `/live` guarded one-shot bridge | Record one exact `v3` proposal, verify evidence and a separate signed exact-plan approval, arm one exact plan, submit once through the existing live leg, reconcile by the same request id | Start a continuous AX-SCALPER session, promote a PAPER result, widen a risk limit, re-send an ambiguous request, close a broker position |

An eligible PAPER admission result is only one prerequisite read by the LIVE
bridge. The PAPER host's live handoff remains blocked. The bridge reads the host's
authoritative status; it never changes the PAPER host, never edits evidence, and
never opens the handoff.

## Operator prerequisites (Windows / MT5)

1. Use the project `.venv` on Windows, with `MetaTrader5` and `tzdata`
   installed as described in the [workstation setup](SCALPER_AUTOPILOT_SETUP.md)
   and the main `README.md`.
2. Open MetaTrader 5 from your broker, sign in **inside the terminal**, and leave
   it running. AXIOM reuses that session and never asks for your MT5 password.
3. Keep `mt5_settings.local.json` beside the settings file. Live execution needs
   `"mode": "TRADING_ENABLED"`; `OBSERVE_ONLY` leaves LIVE blocked. Broker-side
   stop distances are still configured from `/live`.
4. Configure `provider_setup.local.json` with the exact broker symbol, costs,
   calendar, risk limits and quote-freshness policy. No value is inferred.
5. Supply an independent signed-replay admission policy and artifacts beside the
   settings file:
   - `scalper_admission.local.json` — explicit thresholds and trusted reviewer
     public keys, no assumed defaults;
   - `scalper_evidence/` — versioned signed artifacts, one per exact strategy
     fingerprint, independently replayed and digest-verified.
6. Create `scalper_live_approval.local.json` beside the settings file. It must be
   signed by a trusted reviewer who is independent of the strategy developers,
   and must repeat the exact plan fingerprint and every exact typed-intent field.
   Its schema is `axiom-scalper-live-approval-v1`, phrase
   `APPROVE THIS EXACT LIVE PLAN`, and canonical signature excludes the
   `signature` field. No credential field is accepted.
7. Stop any continuous `/live` session. The bridge refuses a one-shot intent
   while a continuous session is running or could send on the next signal.

All of these `*.local.json` files and the evidence directory are Git-ignored.
They never contain an MT5 password or account number. The MT5 password belongs
to the terminal and the OS vault.

## The six separate decisions on `/live`

The **Guarded AX-SCALPER one-shot LIVE intent** card has six actions. Each is a
separate POST with the per-process nonce; none is a shortcut around the others:

1. **Record exact proposal** — paste one `axiom-scalper-live-intent-v3` JSON
   object. It must declare the exact candidate and evidence fingerprints,
   account/instrument/risk/config fingerprints, broker symbol, `quote_received_at`,
   declared quantity **and its unit**, stop, target, bid, ask, source, expiry and
   side. Old `v1`/`v2` proposals are refused; v3 fields are never invented.
2. **Review exact proposal** — tick the acknowledgement, type
   `REVIEW THIS EXACT LIVE PLAN`, and repeat the exact account fingerprint shown
   on `/live`.
3. **Verify signed approval and approve** — repeat the exact plan fingerprint and
   `APPROVE THIS EXACT LIVE PLAN`, and repeat the account fingerprint. The
   independent signature, reviewer independence, validity window and exact
   identity are re-verified.
4. **Arm this exact plan** — type `ENABLE LIVE TRADING` and repeat the account
   fingerprint. This calls the existing `/live` arming workflow with the exact
   plan bindings, then prepares one owner-thread stack without starting a
   strategy runner.
5. **Submit one guarded intent** — one call to the existing `LiveLeg`, through the
   same pre-trade gate, mode router, MT5 adapter write-ahead record and booking
   path. There is no second order route and no retry.
6. **Reconcile the same request id** — query the durable order record and broker
   reconciliation. An ambiguous, duplicate or unresolved request is queried; it
   is never re-sent.

The plan fingerprint binds the typed intent, the exact armed-plan identity and
the verified evidence digest. Every later step re-checks signed evidence,
approval, runtime identity, plan bindings, quote freshness, expiry, account,
mode, connection, armed state, unresolved orders and broker protections.

## What happens after submission

- The one-shot plan is **disarmed** after a broker outcome, a refusal, an
  ambiguous outcome or a duplicate query. It does not become a continuous live
  session.
- A partial fill, rejection, booking failure, unresolved outcome or gate refusal
  is recorded durably with the broker's own answer. Only `FILLED` or `PARTIAL`
  count as a successful submission; no P&L is invented.
- The card shows broker-reported **open-position profit only**. It does not read
  balance, equity or closed P&L, and it does not label an unavailable figure as
  zero.
- Stopping, restarting or disconnecting does not close or reassign positions.
  Open broker positions remain yours to manage in MetaTrader 5.
- Recovery reads the durable proposal snapshot and the same request id. A
  `SUBMITTING` state after a restart is treated as in doubt and reconciled; no
  new broker request is created.

## Update without disturbing local state

Close the workstation before updating. On the active feature branch, use the
in-place updater, which fetches and fast-forwards only, leaves untracked and
ignored files alone, and refreshes the editable install in `.venv`:

```bat
scripts\update-workstation.cmd
```

The equivalent manual fast-forward is:

```bat
git fetch origin arena/51d820ec-axiom-forex
git merge --ff-only origin/arena/51d820ec-axiom-forex
.venv\Scripts\python.exe -m pip install -e . --no-deps
```

Do not `reset`, `clean`, stash, reclone or overwrite local settings. Preserve
`mt5_settings.local.json`, `provider_setup.local.json`,
`scalper_admission.local.json`, `scalper_evidence/`,
`scalper_live_approval.local.json`, all `*.local.json` documents, the SQLite
ledger and the PAPER ledgers. Re-read `/live` after the update: arming is an
in-process decision and is never restored automatically.

## What this does not claim

The bridge does not make a strategy profitable or live-ready, does not create
independent evidence, does not approve itself, and does not prove that a real
broker will accept a stop or fill an order. A real MT5 account was never
connected, armed or traded while implementing or testing this path. This feature
exists to make a deliberate, exact, one-shot decision auditable and fail-closed —
not to make LIVE trading easier to reach by accident.
