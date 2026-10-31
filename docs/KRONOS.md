# Kronos: an optional forecasting component

Kronos is a foundation model that predicts the next few K-lines. AXIOM can run it
as an **optional, replaceable forecasting component** with its own page. It is not
a strategy, not a signal, and not an oracle: the page cannot place an order, size a
position, set a stop, or change an execution mode.

Everything below was read from the official repository, not inferred from the
model's name or its demo screenshots. `axiom_forex/kronos/registry.py` holds the
same facts in code, with the commit they were read at.

## 1. What it is, and under what licence

| Fact | Value |
| --- | --- |
| Source | `github.com/shiyu-coder/Kronos` at commit `67b630e` |
| Paper | arXiv `2508.02739` |
| Code licence | MIT ("Copyright (c) 2025 ShiYu") |
| Weights | published on Hugging Face under `NeoQuasar/*` |
| Weights licence | **`UNVERIFIED_BY_AXIOM`** — confirm it on the model card yourself |

The official model zoo, exactly as the README lists it:

| Variant | Weights | Context | Parameters |
| --- | --- | --- | --- |
| `Kronos-mini` | public | 2048 | 4.1M |
| `Kronos-small` | public | 512 | 24.7M |
| `Kronos-base` | public | 512 | 102.3M |
| `Kronos-large` | **not open-source** | 512 | 499.2M |

`Kronos-large` is listed by the project and is **not selectable** here: there is no
official weight file to point at, so the page refuses it rather than pretending.

## 2. Getting it running (all manual, by design)

AXIOM never downloads code, weights, or packages, and never installs software.

1. Get the official code at the pinned commit and its weights from the official
   host. Keep the weights' licence in mind — AXIOM cannot vouch for it.
2. Install the official runtime requirements yourself. They are listed on the page
   with a per-package state, and `torch>=2.0.0` is what usually decides whether it
   can run at all:

   ```
   numpy, pandas==2.2.2, torch>=2.0.0, einops==0.8.1, huggingface_hub==0.33.1,
   matplotlib==3.9.3, tqdm==4.67.1, safetensors==0.6.2
   ```

   Only `torch`, `pandas`, `numpy`, `einops`, `safetensors` and `huggingface_hub`
   are needed to forecast; `matplotlib` and `tqdm` are reported as optional.
3. On `/kronos`, set **Official code checkout** to the repository folder (it must
   contain `model/kronos.py`) and **Model file location** to the weights folder (a
   `config.json` plus a `.safetensors`/`.bin`/`.pth`/`.pt` file).
4. Press **Inspect and hash the model files**. This runs locally, prints each
   file's size and SHA-256, and records them in `kronos_weights.local.json` so you
   can compare them with the publisher's own values. The state stays
   `UNVERIFIED_BY_AXIOM`, because AXIOM has nothing upstream to compare against.
5. Press **Load model**. That is the first moment any model file is read.

A Hub reference (`NeoQuasar/Kronos-small`) is accepted only with a **pinned
revision**, and even then it is a read of files already under that revision. There
is no "download" button anywhere in AXIOM.

## 3. Settings, and their limits

Every control maps to a real Kronos argument or to an AXIOM-side scheduling
choice. The context limit comes from the selected variant's published length, never
from a guess.

**Basic** — model variant, model file location, device (`auto` resolves to
`cuda:0`, then `mps`, then `cpu`, exactly as the predictor does), input symbol,
input timeframe (1m/5m/15m/30m/1h/4h/1d), context length in bars, forecast horizon
in bars, forecast rollouts.

**Advanced** — sampling temperature, top-k, top-p, `max_context`, standardisation
`clip`, volume mapping (`TICK_VOLUME` or `ABSENT`), scheduled interval, AXIOM-side
seed, pinned revision, code checkout.

**Not supported, and not offered** (listed on the page, with the reason): a seed
argument, a dtype switch, batch size, beam search, exogenous inputs, tick input,
and a broker-provided `amount`. If you need one of those, it is not a setting to
add here — it is a different model.

### Rollouts, and why they cost what they cost

The predictor averages the paths it is given, so `sample_count=8` returns one
averaged path and throws the spread away. A forecast without an uncertainty
estimate cannot be used by a risk process, so AXIOM runs **one `predict()` per
rollout** and keeps the distribution. That multiplies inference time by the rollout
count, and the page says so before you load.

## 4. What is sent to the model

| Kronos column | AXIOM source | Label |
| --- | --- | --- |
| `open/high/low/close` | `(bid + ask) / 2` per point | `MID` |
| `volume` | the bar's `quote_count` (MT5 tick count) | `TICK_VOLUME` |
| `amount` | `tick_volume × mean(mid OHLC)` | `DERIVED` |
| timestamps | the bar's UTC open time; future stamps are a nominal continuation | `EXACT` / nominal |

Three consequences, stated on the page and stored with every forecast:

* **Mid is not tradable.** A forecast of the mid ignores the spread you pay; the
  strategy layer applies the broker's costs through the existing gate.
* **MT5 tick volume is not exchange volume.** It counts price updates, and it
  varies with feed frequency. It is labelled `TICK_VOLUME`, never called turnover.
* **Nothing absent is invented.** Without a tick count, `volume` and `amount` are
  omitted entirely (`ABSENT`); the predictor's own zero-fill is the predictor's
  behaviour, not this project's data.

An input window that is unordered, incomplete, crossed, duplicated, or too short is
**refused**. Gaps are counted and reported, never interpolated.

## 5. The page

`/kronos` is separate from Provider setup, from Data & evaluation, and from Live
execution. It shows:

* a status chip that only ever says what the runtime reported —
  `NOT_LOADED`, `LOADING`, `READY`, `FAILED` (with a reason), `STOPPED`;
* the hardware and memory **estimate** before loading (weights at fp32, context
  length, rollouts), with the things the estimate does not include listed next to
  it;
* every dependency with its requirement line and whether it is present;
* the recent input chart and, when a forecast has run, the forecast median with
  its 10th–90th percentile band, the forecast timestamp, the data freshness, the
  model and its revision, and the input-quality report;
* the recorded forecast history, with each row's outcome once its horizon has
  elapsed — direction, absolute error, band coverage — and *no* profit figure;
* a comparison against the baselines below, on the chronological out-of-sample
  window only;
* the field-by-field input mapping, and the settings Kronos does not support.

Controls: **Load model**, **Run one forecast**, **Start/Stop scheduled
forecasting**, **Stop and unload**, **Save/Load named presets**, **Score forecasts
whose horizon has passed**, **Run the benchmark**, **Inspect and hash the model
files**.

## 6. The benchmark, and what it is not

For one symbol and one timeframe, over the last 20% of the bars (nothing here is
fitted, so nothing can be tuned on that window), AXIOM walks forward in
non-overlapping steps and asks every model for the same horizon:

| Baseline | What it does |
| --- | --- |
| `hold` | the price does not move — the zero-information floor |
| `drift` | straight-line extrapolation of the last 20 bars' average change |
| `momentum` | the repository's SMA 8/21 direction, carried at the recent average bar move |
| `kronos` | the configured variant and settings, unchanged |

Two figures are reported:

* **forecast error and direction** — mean absolute error of the forecast close, and
  how often the sign of the forecast move matched the realised move;
* **a spread-adjusted directional score** — the price move a one-unit position
  would have captured going with the forecast sign, minus the bid/ask spread
  actually quoted at entry and exit. It is one unit, no sizing, no stop, no swap,
  no commission, in price terms. It isolates whether the direction survives the
  broker's own spread, which is the cheapest question to ask first.

The table is ordered by model name, not by score. **A benchmark score is not
evidence of profit**, and no score here can promote anything.

## 7. Where a forecast could ever reach an order

It cannot, from this page. Using a forecast in trading would require:

1. a separately specified strategy that reads the forecast;
2. an explicit operator selection of that strategy (never inferred from a score);
3. the existing pre-trade risk gate, unchanged;
4. a broker-side protective stop for every position;
5. a PAPER/LIVE mode the operator chose deliberately.

Until results exist on your broker's data, any strategy built on it is
**UNVALIDATED**. Nothing in this package is a claim that Kronos forecasts
profitably.

## 8. Files, and what is in Git

| File | Contents | In Git |
| --- | --- | --- |
| `axiom_forex/kronos/*.py` | the component, its tests' subject | yes |
| `kronos_settings.local.json` | the settings you saved | no (`*.local.json`) |
| `kronos_presets.local.json` | named presets | no |
| `kronos_weights.local.json` | local file digests | no |
| `kronos_benchmark.local.json` | the last benchmark result | no |
| `kronos_forecasts.sqlite3` | forecast history and outcomes | no (`*.sqlite3`) |

No setting, preset, history row, or log contains a credential: there are none to
contain.

## 9. What cannot be verified from a Linux sandbox

Every actual Kronos run — loading weights, `torch` device selection, inference
time, memory use — needs the operator's machine. The tests here use a fake
predictor and synthetic bars; they pin the contract, not the model.
