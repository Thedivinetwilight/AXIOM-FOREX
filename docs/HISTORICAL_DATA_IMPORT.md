# Historical data import (offline, user-supplied)

AXIOM-FOREX can import user-supplied historical forex data through one
documented, provider-neutral canonical CSV. The importer is offline: it reads a
local text file, performs no network call, reads no credentials, connects to no
account, and never enables external execution or a live order path.

Imported data is **research/backtest material**. It is never used for paper
fills, is never relabelled as a synthetic fixture or a production feed, and no
historical or simulated result is evidence of profitability or live readiness.

- Format identifier: `AXIOM_FOREX_CANONICAL_CSV`
- Schema version: `1.0.0` (`SUPPORTED_IMPORT_SCHEMA_VERSIONS = {"1.0.0"}`)
- Manifest schema: `import-manifest-v1`
- Data classes accepted by the importer: `USER_SUPPLIED_DEMO`,
  `USER_SUPPLIED_HISTORICAL`, `USER_SUPPLIED_PRODUCTION`
- `SYNTHETIC_FIXTURE` is rejected as an import data class: a file cannot claim to
  be a locally generated fixture.

An imported dataset can be passed to the
[multi-pair research evaluation](RESEARCH_EVALUATION.md), which simulates a
shared multi-currency portfolio across pairs, aligns each pair's calendar and
missing intervals explicitly, and never creates an order.

## Why a canonical format instead of a vendor format

No broker, vendor, or data provider has been selected, and different vendors
export different column names, timestamps, timezones, and quote semantics. The
canonical format fixes the *meaning* of each field (separate executable bid/ask,
explicit timezone, completed bars only, explicit receive times and quote ids) so
that normalization, calendar checks, quality checks, conformance, backtests, and
reports all consume the same shape. A vendor-specific export adapter would be a
separate, reviewed addition and is not included.

## File layout

UTF-8 text. Lines beginning with `#` and blank lines are ignored. The first
remaining line is the header; the column *set* must match exactly (any order):

```
record_type,pair,timestamp,timezone,bid,ask,quote_id,received_at,correction_of,
timeframe,duration_seconds,bid_open,bid_high,bid_low,bid_close,
ask_open,ask_high,ask_low,ask_close,quote_count,last_quote_at,complete
```

Each remaining line is one record:

- `QUOTE` — a timestamped executable bid/ask quote. Requires `pair`,
  `timestamp`, `timezone`, `bid`, `ask`, `quote_id`, `received_at`.
  `correction_of` is optional. Bar columns must be empty.
- `BAR` — a completed bid/ask bar. Requires `pair`, `timestamp`, `timezone`,
  `timeframe`, `duration_seconds`, `bid_open/high/low/close`,
  `ask_open/high/low/close`, `quote_count`, `last_quote_at`, `complete`.
  Quote columns must be empty.

Row order is not significant; records are validated independently and then
sorted chronologically. See `imports/SAMPLE_canonical_quote_bars.csv` for a
format example (illustrative values only, imported as `USER_SUPPLIED_DEMO`).

## Timestamp and timezone semantics

- `timestamp` is ISO 8601 and is either naive wall time in the row's `timezone`
  or offset-aware.
- `timezone` must be `UTC` or a known IANA zone name. There is no fallback zone.
- A naive wall time that is **ambiguous** (DST fall-back) or **nonexistent**
  (DST spring-forward) is rejected. Supply an explicit UTC offset instead; it is
  never guessed.
- An offset-aware timestamp must agree with the declared zone at that instant
  (`timestamp_offset_mismatch` otherwise). `2026-03-10T09:50:00` in
  `America/New_York` resolves to `2026-03-10T13:50:00Z`; the same wall time on
  `2026-03-06` resolves to `14:50Z`.
- `received_at` must not precede the quote timestamp; `last_quote_at` must fall
  inside the bar interval.
- The dataset's declared timezone basis is `UTC`; all stored bars and quotes are
  UTC.

## Required import declarations

An import is rejected unless every declaration is explicit (`ImportRequest`):

| Declaration | Meaning |
| --- | --- |
| `pair` | Canonical `BASE/QUOTE`; must exist in the versioned catalog and the profile's supported pairs |
| `timeframe` | Must be listed in the profile's supported timeframes |
| `data_class` | One of the three `USER_SUPPLIED_*` classes |
| `source` | Documentation-style provenance label (no paths, no URLs) |
| `source_version` | Source/export version identifier |
| `documented_at` | Optional documentation date |
| `valid_from`, `valid_until` | Data validity bounds, half-open `[valid_from, valid_until)` |

Nothing is defaulted. A source label that looks like a local path or URL is
rejected (`unsafe_source_label`) so reports and manifests cannot leak local
paths.

## Compatibility gates (before any record is trusted)

`check_import_compatibility` compares the request against the active provider
profile, the versioned pair spec, and the trading calendar:

`profile_validated`, `profile_identity`, `pair_supported`, `pair_alias`,
`provider_id`, `timeframe_supported`, `instrument_spec`, `calendar_identity`,
`calendar_validity`, `history_bounds`, `timestamp_basis`, `quote_semantics`
(separate bid/ask, quote ids, receive times, explicit corrections),
`bar_semantics` (completed bid/ask bars, no interpolation), `data_class`
(production data cannot be validated against a synthetic/demo profile), and
`spread_limit`.

Any failure rejects the whole import with reason
`profile_incompatible:<check>`. A missing or unrecognized term fails closed.

## Record validation and quarantine

Rejected (whole import fails, no dataset is produced):

- malformed rows, unknown/duplicate/missing header columns, unknown record types;
- unknown pair or a pair that differs from the declaration (`unexpected_pair`);
- invalid, ambiguous, nonexistent, mismatched-offset, or out-of-bounds
  timestamps;
- non-numeric, non-positive, or crossed bid/ask (`non_positive_bid`,
  `crossed_quote`);
- quotes whose receive time violates the freshness policy (`stale_quote`);
- bars that are incomplete (`incomplete_bar`), have stale `last_quote_at`
  (`STALE_OR_INVALID_LAST_QUOTE`), or exceed the profile spread limit
  (`spread_limit_exceeded`);
- duplicate quote ids or timestamps with different values (`conflicting_quote`,
  `conflicting_quote_timestamp`), or two different bars at one opening timestamp
  (`conflicting_bar`);
- corrections referencing an unknown quote id (`unknown_correction_target`);
- bars outside the calendar sessions.

Preserved as explicit quality events (never silently repaired):

- identical duplicates (`DUPLICATE_QUOTE`, `DUPLICATE_QUOTE_TIMESTAMP`,
  `DUPLICATE_BAR`) — kept once, counted in the manifest;
- explicit corrections (`QUOTE_CORRECTION`) — accepted and counted;
- missing bars in an open session (`MISSING_BAR`) and expected session/weekend
  gaps (`EXPECTED_SESSION_GAP`). A dataset with missing bars is `QUARANTINED`:
  it is returned for inspection with `usable = False`, so the backtest engine
  rejects it. No price, bar, quote, or fill is ever interpolated or inferred
  from a mid price.

Every rejected row is returned in `quarantine` with its source line, record
type, and finding codes (no values are echoed). Result statuses are `PASS`,
`QUARANTINED`, or `FAIL`.

## Dataset manifest

Every accepted import produces an `ImportManifest` plus a `DatasetManifest`
with:

- format and schema version, import id, pair, timeframe, data class;
- source label, source version, documentation date, timezone basis;
- declared validity bounds;
- `source_sha256` (raw file) and `source_byte_count`;
- `normalized_sha256` (bars), `quotes_sha256` (quotes), `bar_count`,
  `quote_count`, `row_count`;
- pair spec version and signature, catalog version and signature;
- calendar id, version, and signature;
- time coverage (`first_timestamp`, `last_timestamp`, quote coverage);
- validation classification, pass flag, error/warning codes, correction and
  duplicate counts;
- profile id and version.

`ImportManifest.verify(...)` recomputes the digests and re-checks counts,
coverage, source, pair, timeframe, and data class. A tampered, relabelled, or
truncated manifest does not verify, and the dataset becomes unusable.

The dataset manifest is persisted in the existing `datasets` table and the
import report in a `snapshots` row of kind `import`. The data class and
provenance therefore survive restart and appear in `status`, `/dataset`,
`/status`, the dashboard, the Markdown report, and desktop view models.

## Using an imported dataset

```bash
python -m axiom_forex import \
  --import-path imports/SAMPLE_canonical_quote_bars.csv \
  --import-data-class USER_SUPPLIED_DEMO \
  --import-source SAMPLE_CANONICAL_CSV \
  --import-source-version sample-v1 \
  --import-valid-from 2026-03-10T13:00:00Z \
  --import-valid-until 2026-03-10T14:00:00Z
```

Add `--import-backtest` to run one deterministic backtest on the imported
dataset. Programmatically:

```python
result = platform.import_dataset(request, path=".../quotes.csv")
if result.passed:
    platform.use_imported_dataset()          # research/backtest adoption
    backtest = platform.run_imported_backtest(strategy)
```

Adoption requires the same pair spec and a timeframe supported by the platform.
While imported data is adopted:

- `platform.data_class` is the imported class and `execution_eligible` is `False`;
- `execute_paper_workflow` blocks with `data_class_not_execution_eligible`
  before qualification, routing, or execution;
- backtests and reports run normally and carry the imported dataset id, hashes,
  and provenance.

## Exporting from MetaTrader 5

Nothing in this repository downloads history. If you have a licensed export from
your own MetaTrader 5 terminal, you convert it to the canonical CSV yourself (or
with a reviewed script of your own) and import it offline. Three practical points:

1. **Bar exports from MT5 are not bid/ask bars.** `copy_rates_from` /
   `copy_rates_range`, and the terminal's "Save as CSV" for a chart, return one
   OHLC series plus tick volume and a spread column. There is no separate bid and
   ask OHLC. Because this format requires *executable* two-sided prices and the
   evaluation fills at the ask for a buy and the bid for a sell, a single-series
   export cannot be silently split into bid/ask: any such split invents the
   spread. Use a **tick export** (`copy_ticks_range`: `time`, `bid`, `ask`,
   `last`, `volume`, `flags`) and aggregate it into complete 1-minute bars
   yourself, recording `quote_count`, `last_quote_at`, and `complete=true` only
   for windows that actually received ticks.
2. **Timestamps.** MT5 times are the terminal's server time, not UTC. Record the
   server timezone offset you used and convert to UTC; put the UTC instant in
   `timestamp` and `UTC` in `timezone`. A naive or mislabelled conversion is the
   most common way a "profitable" backtest is born, and the importer rejects
   ambiguous or nonexistent local times rather than guessing.
3. **Declarations are yours.** The pair, timeframe, data class, source label,
   source version, and validity bounds are command arguments at import time. A
   local path is never treated as provenance. Source labels must be plain
   identifiers (`MY_BROKER_EXPORT`), not paths or URLs.

What the importer will not accept, and why:

| Rejected | Reason |
| --- | --- |
| A single mid-price series | no executable side; a spread would have to be invented |
| Bars with `complete=false` or a missing `quote_count` | a partial window is not a bar |
| Naive timestamps, DST-ambiguous local times, mismatched offsets | the instant is unknown |
| Crossed quotes (bid > ask) | not a tradeable market |
| Rows with unknown columns or record types | the format is exact, and guessing hides errors |

The result is a dataset with a manifest, hashes, quality events, and a data class.
`USER_SUPPLIED_DEMO` and `SYNTHETIC_FIXTURE` data can be studied but can never
select a strategy for paper running; only `USER_SUPPLIED_HISTORICAL` and
`USER_SUPPLIED_PRODUCTION` can. See [PAPER_RUNNER.md](PAPER_RUNNER.md).

## Converting a tick export (the only supported MT5 route)

`python -m axiom_forex.ingest.mt5_ticks`'s command wrapper does the conversion:

```bash
python -m axiom_forex.cli convert-ticks \
  --ticks-path C:/exports/EURUSD_2026-03-10.csv \
  --ticks-pair EUR/USD \
  --ticks-server-offset-minutes 120 \
  --ticks-interval-seconds 60
```

It writes `<name>_canonical.csv` (the canonical format above) next to the export,
plus `<name>_canonical.csv.provenance.json`. Pass `--ticks-server-timezone
Europe/Athens` instead of a fixed offset when the server follows a DST zone: the
offset form is a constant, and the zone form is refused for a wall time that is
ambiguous or nonexistent.

What it does, and what it refuses:

| Step | Rule |
| --- | --- |
| Header | MetaTrader's bracketed tabs (`<DATE> <TIME> <BID> <ASK> <LAST> <VOLUME> <FLAGS>`) or the same names comma-separated; a file without bid *and* ask is not a tick export and is refused |
| Time | Every row is server time. The offset (or an IANA zone) is a required argument; a wall time that is ambiguous or nonexistent under the declared zone is refused, never resolved by a guess |
| Prices | Non-positive or crossed quotes are rejected and counted; a duplicate timestamp with a different price is a conflict, not an overwrite |
| Order | Rows must already be sorted; an unsorted export is refused so nothing reorders your data behind your back |
| Bars | Bid OHLC from bid ticks, ask OHLC from ask ticks, `quote_count`, `last_quote_at`, and `complete=true` only for windows the export actually filled |
| Gaps | A window with no ticks is reported and **not** emitted. Coverage lists every empty window and the largest jump between two non-empty windows, in seconds |
| Truncation | The final window is dropped when the file ends inside it, and is reported as `trailing_partial_window` |
| Provenance | The sidecar records the source file name, its SHA-256, byte count, tick count, duplicates, rejected rows, coverage, the declared time basis, and `derivation: DERIVED_FROM_BROKER_TICKS`; nothing in it is a price the broker did not export |

Then import the result with the declarations you choose:

```bash
python -m axiom_forex.cli paper-evaluate \
  --import-path C:/exports/EURUSD_2026-03-10_canonical.csv \
  --import-pair EUR/USD --import-timeframe 1m \
  --import-data-class USER_SUPPLIED_HISTORICAL \
  --import-source YOUR_BROKER_TICK_EXPORT --import-source-version v1 \
  --import-valid-from 2026-03-10T00:00:00Z --import-valid-until 2026-03-11T00:00:00Z
```

A gap is not silently repaired at import time either: the importer raises
`MISSING_BAR`, marks the dataset `QUARANTINED`, and the evaluation refuses it. Fix
the export (or accept a shorter window), do not fill the hole.

To see what you actually have before importing anything:

```bash
python -m axiom_forex.cli data-audit --evaluate-markdown
```

## Provider-specific gaps

The importer does not assume any vendor. Still required once a provider is
selected:

1. A reviewed export adapter from that vendor's format into this canonical CSV
   (column mapping, timestamp semantics, correction/restatement handling).
2. The provider's official documentation source and effective dates for each
   profile term used by the compatibility gates.
3. Whether the provider supplies receive times and quote ids; if not, how
   restatements are identified and how the receive-time freshness policy maps to
   historical exports.
4. Historical depth, bar completion, gap, and correction policies per timeframe.
5. Tick/pip precision, price alignment, and maximum spread terms that the import
   gate should enforce for that instrument.
6. Provider holiday/DST calendars and any per-instrument availability windows
   used by the calendar gate.
7. Sampling/quoting conventions for user-generated data (aggregated liquidity,
   mid-only feeds are not acceptable: separate executable bid/ask is required).

Until those are supplied, imports remain offline research artifacts and no
imported data can reach the local paper execution path.

## Doing this in the workstation instead of the CLI

`scripts\start-workstation.cmd` (or `python -m axiom_forex.workstation.launch`) serves the same
workflow at `/data`:

| Step | CLI equivalent | What the page will not do |
| --- | --- | --- |
| List the folder | `data-audit` | download, recurse into sub-folders, or accept a path from a form |
| Convert a tick export | `convert-ticks` | guess the pair, the interval, or the server's time basis |
| Evaluate a canonical file | `paper-evaluate` | pick a data class for you, or re-run a spent locked test |

The page reports the same coverage, the same rejected rows, and the same evidence the CLI records,
because it calls the same functions rather than shelling out. A conversion writes the canonical CSV
and its `.provenance.json` sidecar into the folder it read from; nothing else is created.

