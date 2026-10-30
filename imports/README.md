# `imports/` — user-supplied data drop point

This directory is intentionally separate from `fixtures/` (locally generated
synthetic fixtures) and `profiles/` (provider onboarding documents). Nothing in
this repository reads these files at runtime; imports are explicit,
user-initiated, and offline.

- `SAMPLE_canonical_quote_bars.csv` is a **format example only**. Its AAA/BBB
  values are illustrative and must be imported as `USER_SUPPLIED_DEMO`, never as
  production or fixture data.
- Real user data can be placed here, but the importer never treats a local path
  as provenance: the declared `source` label, `source_version`, data class, and
  validity bounds are explicit command arguments and become part of the dataset
  manifest.
- Do not place credentials, account exports, or anything containing sensitive
  identifiers in this directory. The canonical format has no field for them, and
  the importer rejects source labels that look like paths or URLs.

See [Historical data import](../docs/HISTORICAL_DATA_IMPORT.md) for the full
format, validation rules, quotas, and manifest fields.
- Several canonical CSVs can be imported and then evaluated together. See
  [Multi-pair research evaluation](../docs/RESEARCH_EVALUATION.md); the
  evaluation consumes only imported datasets, never this directory's sample
  fixture as production data.
- A broker tick export is converted into this format with
  `python -m axiom_forex.cli convert-ticks --ticks-path ... --ticks-pair ... --ticks-server-offset-minutes ...`
  (or `--ticks-server-timezone`), which writes the canonical CSV and a provenance sidecar next to
  it. Only the broker's two-sided ticks become bars: a candle export is refused, empty windows are
  reported as gaps and never filled, and the final partial window is dropped and named. See
  [Converting a tick export](../docs/HISTORICAL_DATA_IMPORT.md#converting-a-tick-export-the-only-supported-mt5-route).
- To see what this directory currently holds, and whether it is a format example
  or real history, run `python -m axiom_forex.cli data-audit --evaluate-markdown`.
  The audit only reads; it never downloads, guesses, or repairs a file.
