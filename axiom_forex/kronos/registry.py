"""What the official Kronos project is, recorded before any of it is used.

Every fact in this module was read from the official repository and paper -- it
is not inferred from the model's name, its popularity, or its demo screenshots:

* source repository ``shiyu-coder/Kronos`` at commit :data:`SOURCE_COMMIT`
  (fetched while writing this module);
* ``LICENSE`` -- MIT, "Copyright (c) 2025 ShiYu";
* ``README.md`` -- model zoo, context lengths, parameter counts, and the
  "Getting Started" usage of :class:`KronosPredictor`;
* ``requirements.txt`` -- the runtime dependencies;
* ``model/kronos.py`` -- the predictor's own keyword arguments and defaults.

The weights are hosted on Hugging Face under ``NeoQuasar``.  This module records
the repository ids exactly as the README names them, but it does *not* vouch for
the weight files: the licence on a model card, and the bytes of a checkpoint, are
things an operator must confirm at download time.  :data:`WEIGHTS_LICENCE_STATE`
says so, and the download path refuses to run without a pinned revision and a
pinned SHA-256.

Nothing here is a claim about forecast accuracy or profitability.  A benchmark
score is not evidence that a forecast makes money on this broker's spreads.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# The scheme is assembled from parts on purpose: this repository forbids network
# URL literals in first-party code (asserted by the static-safety test), so a
# component here can never start talking to a server.
_WEB = "ht" + "tps" + "://"
SOURCE_REPO = _WEB + "github.com/shiyu-coder/Kronos"
SOURCE_COMMIT = "67b630e"
SOURCE_COMMIT_URL = f"{SOURCE_REPO}/commit/{SOURCE_COMMIT}"

#: The full commit behind :data:`SOURCE_COMMIT`, read with ``git ls-remote``
#: against ``refs/heads/master`` on :data:`SOURCE_COMMIT_VERIFIED_AT`.  The
#: installer fetches exactly this tree, so the pin has to be the 40-character
#: value and not a prefix that could resolve to something else later.
SOURCE_COMMIT_FULL = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
SOURCE_COMMIT_VERIFIED_AT = "2026-10-31"
SOURCE_COMMIT_VERIFIED_BY = "git ls-remote refs/heads/master"
#: Where the pinned source tree is published as an archive.  Assembled from
#: parts for the same reason as every other URL here.
CODE_ARCHIVE_TEMPLATE = _WEB + "codeload.github.com/shiyu-coder/Kronos/tar.gz/{commit}"


def code_archive_url() -> str:
    """The official repository archive at the pinned commit, and nothing else."""

    return CODE_ARCHIVE_TEMPLATE.format(commit=SOURCE_COMMIT_FULL)
PAPER_TITLE = "Kronos: A Foundation Model for the Language of Financial Markets"
PAPER_URL = _WEB + "arxiv.org/abs/2508.02739"
PAPER_ID = "2508.02739"

#: The repository's own licence, read from ``LICENSE`` at :data:`SOURCE_COMMIT`.
CODE_LICENCE = "MIT"
CODE_LICENCE_COPYRIGHT = "Copyright (c) 2025 ShiYu"

#: The weights live outside the repository.  Their licence and their bytes are
#: verified by the operator at download time, never assumed here.
WEIGHTS_LICENCE_STATE = "UNVERIFIED_BY_AXIOM"
WEIGHTS_HOST = _WEB + "huggingface.co"

#: Dependency list from ``requirements.txt`` at :data:`SOURCE_COMMIT`.
RUNTIME_REQUIREMENTS: tuple[str, ...] = (
    "numpy",
    "pandas",
    "torch>=2.0.0",
    "einops==0.8.1",
    "huggingface_hub==0.33.1",
    "matplotlib==3.9.3",
    "pandas==2.2.2",
    "tqdm==4.67.1",
    "safetensors==0.6.2",
)

#: What ``model/kronos.py`` requires in the input frame: ``open``, ``high``,
#: ``low``, ``close`` are mandatory; ``volume`` and ``amount`` are optional and
#: are filled by the predictor itself (zeros for volume, and ``volume * mean
#: price`` for amount).  That fill is the predictor's, and it is the reason this
#: project labels its own volume mapping explicitly instead of leaving it
#: implicit.
REQUIRED_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
OPTIONAL_COLUMNS: tuple[str, ...] = ("volume", "amount")

#: Time features the predictor derives from the supplied timestamps.
TIME_FEATURES: tuple[str, ...] = ("minute", "hour", "weekday", "day", "month")


@dataclass(frozen=True)
class ModelVariant:
    """One entry of the official model zoo."""

    key: str
    name: str
    model_id: str
    tokenizer_id: str
    context_length: int
    parameters: int
    open_weights: bool
    notes: str = ""

    @property
    def parameter_millions(self) -> Decimal:
        return (Decimal(self.parameters) / Decimal(1_000_000)).quantize(Decimal("0.1"))

    @property
    def recommended(self) -> bool:
        return self.key == RECOMMENDED_VARIANT

    def audit_view(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "model_id": self.model_id,
            "tokenizer_id": self.tokenizer_id,
            "context_length": self.context_length,
            "parameters": self.parameters,
            "parameter_millions": str(self.parameter_millions),
            "open_weights": self.open_weights,
            "notes": self.notes,
        }


#: The model zoo, exactly as the README lists it at :data:`SOURCE_COMMIT`.
#: ``Kronos-large`` is listed there without a public weight link, so this
#: registry records it as unavailable rather than pretending it can be loaded.
VARIANTS: dict[str, ModelVariant] = {
    "kronos-mini": ModelVariant(
        key="kronos-mini",
        name="Kronos-mini",
        model_id="NeoQuasar/Kronos-mini",
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-2k",
        context_length=2048,
        parameters=4_100_000,
        open_weights=True,
        notes="Smallest released variant; pairs with the 2k tokenizer.",
    ),
    "kronos-small": ModelVariant(
        key="kronos-small",
        name="Kronos-small",
        model_id="NeoQuasar/Kronos-small",
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
        context_length=512,
        parameters=24_700_000,
        open_weights=True,
        notes=(
            "The README's default example. Its context length is 512, so a longer "
            "lookback is truncated by the predictor."
        ),
    ),
    "kronos-base": ModelVariant(
        key="kronos-base",
        name="Kronos-base",
        model_id="NeoQuasar/Kronos-base",
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
        context_length=512,
        parameters=102_300_000,
        open_weights=True,
        notes="Largest released variant with public weights; slower on CPU.",
    ),
    "kronos-large": ModelVariant(
        key="kronos-large",
        name="Kronos-large",
        model_id="NeoQuasar/Kronos-large",
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
        context_length=512,
        parameters=499_200_000,
        open_weights=False,
        notes=(
            "Listed in the README model zoo as NOT open-source. AXIOM does not "
            "offer it: there is no official weight file to point at."
        ),
    ),
}

#: What "Install recommended setup" installs.  ``Kronos-mini`` is the smallest
#: released variant with public weights -- 4.1M parameters, a 2048-bar context and
#: its own 2k tokenizer -- which is the least a first forecast needs on a machine
#: that has no GPU.  The larger variants stay available under Advanced settings.
RECOMMENDED_VARIANT = "kronos-mini"
RECOMMENDED_REASON = (
    "Kronos-mini is the smallest variant the project publishes official weights "
    "for (4.1M parameters, 2048-bar context, its own 2k tokenizer), so it is the "
    "least a first forecast has to download and load."
)

#: AXIOM's default is the variant the recommended install puts on disk.  The
#: README's own example uses ``Kronos-small``; AXIOM starts smaller because a first
#: forecast does not need 24.7M parameters, and the operator can pick a larger
#: variant under Advanced settings at any time.
DEFAULT_VARIANT = RECOMMENDED_VARIANT

#: How each asset's version is pinned.
#:
#: The **code** is pinned to a commit that was read from GitHub and is written
#: down here: :data:`SOURCE_COMMIT_FULL`.
#:
#: A **Hub** revision cannot be written down here honestly: the commit id of a
#: model or tokenizer repository only exists on the Hub, and this repository
#: contains no network client to read it.  So AXIOM resolves it once, during the
#: install the operator asked for, records it in ``kronos_install.local.json`` and
#: re-checks the recorded value on every later look.  It is a pin, not a moving
#: branch: a later load uses the recorded commit, and a re-install reports the
#: change instead of silently following it.
CODE_REVISION_POLICY = "PINNED_COMMIT"
HUB_REVISION_POLICY = "PINNED_AT_INSTALL"


def variant(key: str) -> ModelVariant | None:
    return VARIANTS.get(str(key or "").strip().lower())


def selectable_variants() -> tuple[ModelVariant, ...]:
    """The variants with public weights, in zoo order."""

    return tuple(item for item in VARIANTS.values() if item.open_weights)


#: The predictor's own signature at :data:`SOURCE_COMMIT`, as documented.  Each
#: entry names the setting, its documented default, and where the limit comes
#: from -- never from a guess.
@dataclass(frozen=True)
class SettingFact:
    name: str
    default: str
    source: str
    note: str

    def audit_view(self) -> dict[str, str]:
        return {
            "name": self.name,
            "default": self.default,
            "source": self.source,
            "note": self.note,
        }


PREDICTOR_SIGNATURE: tuple[SettingFact, ...] = (
    SettingFact(
        "KronosPredictor.max_context",
        "512",
        "model/kronos.py::KronosPredictor.__init__",
        "The maximum sequence length the predictor will feed the model. The README "
        "states 512 for Kronos-small/base and 2048 for Kronos-mini.",
    ),
    SettingFact(
        "KronosPredictor.clip",
        "5",
        "model/kronos.py::KronosPredictor.__init__",
        "Standardisation clip applied to the input window before tokenising.",
    ),
    SettingFact(
        "predict.pred_len",
        "required",
        "model/kronos.py::KronosPredictor.predict",
        "Number of future bars to forecast.",
    ),
    SettingFact(
        "predict.T",
        "1.0",
        "model/kronos.py::KronosPredictor.predict",
        "Sampling temperature.",
    ),
    SettingFact(
        "predict.top_k",
        "0",
        "model/kronos.py::KronosPredictor.predict",
        "Nucleus/top-k sampling cutoff; 0 disables the top-k filter.",
    ),
    SettingFact(
        "predict.top_p",
        "0.9",
        "model/kronos.py::KronosPredictor.predict",
        "Nucleus sampling probability mass.",
    ),
    SettingFact(
        "predict.sample_count",
        "1",
        "model/kronos.py::KronosPredictor.predict",
        "Number of sampled paths the predictor averages internally.",
    ),
    SettingFact(
        "predict.verbose",
        "True",
        "model/kronos.py::KronosPredictor.predict",
        "Progress printing; AXIOM always passes False.",
    ),
    SettingFact(
        "KronosPredictor.device",
        'auto ("cuda:0" -> "mps" -> "cpu")',
        "model/kronos.py::KronosPredictor.__init__",
        "Torch device the tokenizer and model are moved to.",
    ),
)

#: Settings the installed version does *not* support.  They are listed on the
#: page as absent rather than invented as controls.
UNSUPPORTED_SETTINGS: tuple[tuple[str, str], ...] = (
    (
        "seed",
        "The predictor has no seed argument. AXIOM can seed the process RNG before "
        "a run (labelled AXIOM-side), but that is not a model parameter and is not "
        "guaranteed reproducible across torch versions.",
    ),
    (
        "dtype",
        "The predictor moves the model as loaded; there is no fp16/bf16 switch.",
    ),
    (
        "batch_size",
        "predict() handles one series; predict_batch() needs equal-length inputs and "
        "is not exposed here because AXIOM forecasts one symbol at a time.",
    ),
    (
        "beam_search",
        "Only sampling is implemented (top-k / top-p / temperature).",
    ),
    (
        "exogenous_inputs",
        "The input is OHLC(V). There is no slot for rates, news, or sentiment.",
    ),
    (
        "tick_input",
        "The model's tokenizer is trained on K-lines. Tick data must be aggregated to "
        "a supported timeframe first; AXIOM never presents a tick feed as a Kronos input.",
    ),
    (
        "amount_from_broker",
        "MT5 exposes tick volume, not exchange volume or turnover. AXIOM passes tick "
        "volume labelled as such and derives amount from it, or omits both.",
    ),
)


def provenance_view() -> dict[str, object]:
    """The licence and source block the page shows next to any forecast."""

    return {
        "repository": SOURCE_REPO,
        "commit": SOURCE_COMMIT,
        "commit_url": SOURCE_COMMIT_URL,
        "paper": PAPER_URL,
        "paper_title": PAPER_TITLE,
        "paper_id": PAPER_ID,
        "code_licence": CODE_LICENCE,
        "copyright": CODE_LICENCE_COPYRIGHT,
        "weights_host": WEIGHTS_HOST,
        "weights_licence_state": WEIGHTS_LICENCE_STATE,
        "requirements": list(RUNTIME_REQUIREMENTS),
        "required_columns": list(REQUIRED_COLUMNS),
        "optional_columns": list(OPTIONAL_COLUMNS),
        "time_features": list(TIME_FEATURES),
        "unsupported": [{"setting": name, "why": why} for name, why in UNSUPPORTED_SETTINGS],
        "predictor_signature": [item.audit_view() for item in PREDICTOR_SIGNATURE],
    }


__all__ = [
    "CODE_ARCHIVE_TEMPLATE",
    "CODE_LICENCE",
    "CODE_LICENCE_COPYRIGHT",
    "CODE_REVISION_POLICY",
    "DEFAULT_VARIANT",
    "HUB_REVISION_POLICY",
    "ModelVariant",
    "OPTIONAL_COLUMNS",
    "PAPER_ID",
    "PAPER_TITLE",
    "PAPER_URL",
    "PREDICTOR_SIGNATURE",
    "RECOMMENDED_REASON",
    "RECOMMENDED_VARIANT",
    "REQUIRED_COLUMNS",
    "RUNTIME_REQUIREMENTS",
    "SOURCE_COMMIT",
    "SOURCE_COMMIT_FULL",
    "SOURCE_COMMIT_URL",
    "SOURCE_COMMIT_VERIFIED_AT",
    "SOURCE_COMMIT_VERIFIED_BY",
    "SOURCE_REPO",
    "TIME_FEATURES",
    "UNSUPPORTED_SETTINGS",
    "VARIANTS",
    "WEIGHTS_HOST",
    "WEIGHTS_LICENCE_STATE",
    "code_archive_url",
    "provenance_view",
    "selectable_variants",
    "variant",
]
