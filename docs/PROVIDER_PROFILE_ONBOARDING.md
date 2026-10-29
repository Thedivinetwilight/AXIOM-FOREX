# Provider profile onboarding

Provider profiles are **offline onboarding documents**. They describe the
values a future integration would need from a chosen market-data provider and
broker. They are not credentials, runtime settings, deployment manifests,
adapter implementations, or live-order configuration.

This repository does not choose a broker, data provider, account currency,
leverage, strategy, or account. It does not contact provider documentation
endpoints, external accounts, or order systems. A completed profile only
becomes useful after a human supplies values from the selected providers'
official documentation and an independent review approves a separate adapter.

## Files

- `profiles/offline_fixture_profile.json`: explicitly synthetic, complete
  fixture profile. It reproduces the current offline workflow and is the only
  profile eligible for local-paper activation.
- `profiles/provider_onboarding_template.json`: empty template with all 37
  required keys from `REQUIRED_PROVIDER_VALUES`. Every value is `null` and
  every provenance field is empty. It intentionally fails validation.
- `axiom_forex/profiles/contracts.py`: versioned schema constants, section
  map, profile/provenance dataclasses, and safe operator report.
- `axiom_forex/profiles/loader.py`: strict JSON/mapping loader, no defaults.
- `axiom_forex/profiles/validation.py`: field validation, staleness checks,
  contradiction checks, compatibility checks, and activation decision.
- `axiom_forex/profiles/fixtures.py`: deterministic fixture/template payload
  builders.
- `tests/test_forex_provider_profiles.py`: focused offline tests.

The machine-readable source of truth for the required keys is
`axiom_forex/conformance/contracts.py::REQUIRED_PROVIDER_VALUES`. The exact
table is documented in [the conformance contract](PROVIDER_CONFORMANCE.md).

## Schema

The top-level format is versioned:

```json
{
  "format": "AXIOM_FOREX_PROVIDER_PROFILE",
  "schema_version": "1.0.0",
  "profile": {
    "profile_id": "provider-onboarding-template",
    "profile_version": "template-v1",
    "environment": "UNSPECIFIED",
    "label": "EMPTY_PROVIDER_ONBOARDING_TEMPLATE",
    "synthetic": false,
    "external_execution_enabled": false,
    "activation_mode": "LOCAL_PAPER",
    "evaluated_at": "",
    "effective_from": "",
    "expires_at": "",
    "review_by": ""
  },
  "sections": {
    "market_data": ["market.provider_id"],
    "broker": ["broker.broker_id"]
  },
  "terms": {
    "market.provider_id": {
      "value": null,
      "provenance": {
        "source": "",
        "source_version": "",
        "effective_date": "",
        "value_class": "",
        "expires_at": "",
        "documented_at": "",
        "review_by": ""
      }
    }
  }
}
```

The example above is abbreviated. A real profile must contain **all 37**
required terms and all seven non-empty sections:

1. `market_data`
2. `broker`
3. `instrument`
4. `calendar`
5. `account_risk`
6. `cost_financing`
7. `recovery`

Sections reference term keys; they do not duplicate values. Validation checks
that every required key is assigned to at least one section and that every
section key resolves to a term.

## How to complete a profile from official documentation

1. **Copy the template.** Do not edit the synthetic fixture profile. Copy
   `profiles/provider_onboarding_template.json` to a new local review file.
2. **Choose a deterministic evaluation time.** Fill `profile.evaluated_at`
   with the ISO 8601 timestamp at which the profile is being reviewed. Staleness
   is evaluated against this explicit time, not the machine clock.
3. **Fill profile identity.** Supply `profile_id`, `profile_version`,
   `label`, `environment`, `effective_from`, `expires_at`, and `review_by`.
   Keep `external_execution_enabled` false and `activation_mode` set to
   `LOCAL_PAPER`. Set `synthetic` true only for an explicitly local synthetic
   fixture profile.
4. **Select the value class.** `environment` and every term's
   `provenance.value_class` must be one of `FIXTURE`, `DEMO`, or `PRODUCTION`.
   Mixed classes are rejected. A demo or production profile can be validated,
   but it is never activated in this repository.
5. **Fill every required value from official documentation.** For each of the
   37 keys, use the selected provider's/broker's official contract
   specifications, instrumentmaster, trading-calendar notices, cost schedule,
   swap/financing table, account-currency terms, margin schedule, order-type
   documentation, and recovery/reconciliation documentation as applicable.
   Do not infer missing values from the fixture profile.
6. **Record provenance for every term.** Each `provenance` object needs:
   - `source`: official document title/section/identifier, not a credential,
     secret, live endpoint, or URL.
   - `source_version` or `effective_date`: a document revision/version and/or
     the date the term became effective. Supplying both is recommended.
   - `value_class`: `FIXTURE`, `DEMO`, or `PRODUCTION`.
   - `expires_at`: when the sourced term expires or must be replaced.
   - `documented_at`: when the source was reviewed.
   - `review_by`: when the term must be reviewed again.
7. **Keep credentials out.** Access tokens, passwords, API keys, private keys,
   account numbers that look like credentials, session cookies, and live
   endpoints must never be placed in a profile, a source field, or the
   repository. They belong to a separate reviewed integration boundary.
8. **Validate offline.** Run:

   ```bash
   python -m axiom_forex profile --profile-path /path/to/profile.json
   ```

   The command reads only the local file and returns a safe JSON report. It
   makes no network calls and places no orders.
9. **Resolve every finding.** The report lists `missing_fields`,
   `invalid_fields`, `stale_fields`, `contradictory_fields`, and
   `compatibility_failures` by field/code. No field values are included.
10. **Do not treat validation as activation.** Only an explicitly synthetic
    fixture profile can activate the offline paper workflow. Demo and
    production profiles are review artifacts only; they cannot enable
    external execution here.

## Provenance and effective-date rules

- `source`, `source_version`/`effective_date`, and `value_class` are mandatory
  for each required term.
- `effective_date`, `expires_at`, `documented_at`, `review_by`, profile
  `evaluated_at`, `effective_from`, `expires_at`, and `review_by` must be
  ISO 8601 strings when supplied.
- A term or profile whose `expires_at` or `review_by` is at or before
  `evaluated_at` is stale and fails closed.
- A term whose `effective_date` is after `evaluated_at` is not yet effective
  and fails closed.
- Placeholder sources such as `TBD`, `TODO`, `UNKNOWN`, `N/A`, or
  `REPLACE_ME` are rejected.
- Source strings that look like URLs or credential-like/account-sensitive
  material are rejected and redacted from operator output.

## Compatibility checks

Validation compares data-provider and broker terms across:

- identity, capability versions, execution mode, and external-order flags;
- canonical pairs, provider aliases, pip/tick size, precision, contract size,
  min/step/max quantity, instrument status, and spec version;
- timeframes, UTC bar timestamps, completed-bar rules, bid/ask OHLC, and gap
  policy;
- quote IDs, receive times, corrections, clock source, and quote timestamp
  basis;
- account currency, conversion source/version, conversion pairs, rate
  timestamps, and conversion freshness;
- calendar identity/version/signature, timezone, validity bounds, windows,
  holidays, DST transitions, daily breaks, weekend gaps, and instrument
  availability;
- costs, spread limits, slippage, financing, swap rates and versions, rollover
  cutoff, and explicit triple-rollover weekday or `null`;
- margin, leverage, max leverage, margin multiplier, margin-call policy, and
  negative-equity policy;
- order types, partial fills, cancellation, reconciliation, idempotency,
  rejection codes, durable snapshot source, and no-resubmit recovery policy.

A contradiction or compatibility failure blocks activation.

## Safe operator output

`status()`, `/status`, `/profile`, `/workflow`, `/desktop`, `/dashboard`,
`/report`, `health()`, `verify()`, the durable profile snapshot, and the CLI
expose the same safe report. It shows:

- profile identity, version, environment, label, and activation status;
- validation status, check count, error count, and explicit evaluation time;
- missing, invalid, stale, contradictory, and compatibility-failure fields;
- per-term status and provenance metadata;
- no term values, credentials, or sensitive account values.

The report is sanitized before persistence and presentation.

## Values still to be supplied after choosing a provider

Supply all 37 keys in [the required-values table](PROVIDER_CONFORMANCE.md#required-provider-specific-values), including:

1. Provider and broker identity, capability versions, supported pairs,
   provider symbols, and timeframes.
2. Quote/bar timestamp basis, clock source, bid/ask semantics, quote IDs,
   receive times, correction policy, bar completion/OHLC/gap policy, history
   bounds, and freshness tolerances.
3. Pip, tick, precision, contract size, quantity unit, min/step/max quantity,
   instrument status, spec version, and source.
4. Calendar timezone, DST-aware UTC windows, holidays, daily breaks, weekend
   gaps, validity bounds, instrument availability, and rollover cutoff.
5. Commission, slippage, spread limits, financing, swap rates and versions,
   and triple-rollover weekday or explicit `null`.
6. Account currency, leverage, max leverage, margin multiplier,
   negative-equity policy, conversion-rate source/version/pairs/timestamps,
   and freshness.
7. Order types, rejection/status mappings, idempotency key/order-ID source,
   partial fills, cancellation confirmation, reconciliation source/retention,
   unknown-outcome policy, and durable recovery behavior.

No production defaults are provided. Missing values must fail closed.

## Verification commands

```bash
python -m pytest -q tests/test_forex_provider_profiles.py
python -m pytest -q
python -m axiom_forex profile
python -m axiom_forex profile --profile-path profiles/provider_onboarding_template.json
python -m axiom_forex verify
python -m axiom_forex workflow --workflow-id profile-smoke
```

All commands are repository-local and offline. They do not contact a provider,
read credentials, change runtime settings, affect the running live system,
enable external orders, or deploy anything. Passing profile validation is not
a claim of profitability, broker certification, or live-trading readiness.
