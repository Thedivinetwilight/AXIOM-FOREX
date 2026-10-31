"""Every inference setting the installed Kronos version actually supports.

The controls here map one-to-one onto ``KronosPredictor.__init__`` and
``KronosPredictor.predict`` (see :mod:`axiom_forex.kronos.registry`), plus the
few things AXIOM must decide around them: which symbol and timeframe to read,
how long a context to send, how many rollouts to sample, and how often a
scheduled forecast runs.

Two rules hold throughout:

* **No invented setting.**  Anything the predictor does not accept is *not* a
  field here, and :data:`axiom_forex.kronos.registry.UNSUPPORTED_SETTINGS` names
  it on the page.
* **No setting implies trading risk.**  Nothing in this document changes a
  position size, a stop distance, or a risk limit.  Those live in the live plan
  and the provider setup, and this page cannot reach them.

Presets are stored in ``kronos_presets.local.json`` beside the launcher (ignored
by Git), because they describe a local model installation, not a repo fact.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from .registry import DEFAULT_VARIANT, selectable_variants, variant

PRESET_SCHEMA = "axiom-kronos-presets-v1"
PRESET_FILENAME = "kronos_presets.local.json"

#: Bar timeframes AXIOM can build a Kronos input from.  The finest is one
#: minute: the terminal feed aggregates ticks into bars, and the predictor's
#: tokenizer expects K-lines.  Tick or sub-minute "contexts" are refused rather
#: than pretended.
TIMEFRAMES: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

#: AXIOM's own guards, not model facts.  They keep a request inside a context
#: window a model can actually read.
MIN_LOOKBACK = 32
MIN_HORIZON = 1
MAX_ROLLOUTS = 64
MIN_ROLLOUTS = 1

DEVICES = ("auto", "cpu", "cuda:0", "cuda:1", "mps")


@dataclass(frozen=True)
class SettingProblem:
    path: str
    message: str

    def audit_view(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _int(value: Any) -> int | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    if parsed != parsed.to_integral_value():
        return None
    return int(parsed)


@dataclass(frozen=True)
class KronosSettings:
    """One complete inference configuration."""

    # -- what to load
    model_key: str = DEFAULT_VARIANT
    #: A local directory, or a Hub id (``NeoQuasar/Kronos-small``).  Empty means
    #: "use the official id from the registry".
    model_path: str = ""
    #: Pinned revision for a Hub download; required for a download, optional for
    #: a local directory.
    revision: str = ""
    #: The official repository checkout that provides the ``model`` package.
    #: Empty means "already importable in this interpreter".
    code_path: str = ""
    device: str = "auto"

    # -- what to read
    symbol: str = ""
    timeframe: str = "1m"
    volume_mode: str = "TICK_VOLUME"

    # -- predictor settings
    max_context: int = 512
    clip: Decimal = Decimal("5")
    lookback: int = 400
    pred_len: int = 24
    temperature: Decimal = Decimal("1.0")
    top_k: int = 0
    top_p: Decimal = Decimal("0.9")
    rollouts: int = 5

    # -- AXIOM-side scheduling (not a model setting)
    interval_seconds: int = 900
    seed: int | None = None

    # -- provenance
    preset: str = ""

    # -- derived -------------------------------------------------------

    @property
    def spec(self):
        return variant(self.model_key)

    @property
    def model_reference(self) -> str:
        item = self.spec
        official = item.model_id if item else ""
        return self.model_path.strip() or official

    @property
    def tokenizer_reference(self) -> str:
        item = self.spec
        return item.tokenizer_id if item else ""

    @property
    def bar_seconds(self) -> int:
        return TIMEFRAMES.get(self.timeframe, 0)

    def fingerprint(self) -> str:
        """A stable fingerprint of everything that changes the numbers."""

        import hashlib

        payload = {
            "model_key": self.model_key,
            "model_reference": self.model_reference,
            "revision": self.revision,
            "code_path": self.code_path,
            "device": self.device,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "volume_mode": self.volume_mode,
            "max_context": self.max_context,
            "clip": str(self.clip),
            "lookback": self.lookback,
            "pred_len": self.pred_len,
            "temperature": str(self.temperature),
            "top_k": self.top_k,
            "top_p": str(self.top_p),
            "rollouts": self.rollouts,
            "seed": self.seed,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]

    # -- validation ----------------------------------------------------

    def validate(self, *, symbols: Iterable[str] = ()) -> tuple[SettingProblem, ...]:
        """Every problem with this configuration, in the operator's terms."""

        problems: list[SettingProblem] = []
        item = self.spec
        if item is None:
            problems.append(
                SettingProblem(
                    "kronos.model",
                    f"{self.model_key} is not a published Kronos variant; choose "
                    + ", ".join(entry.key for entry in selectable_variants()),
                )
            )
        elif not item.open_weights:
            problems.append(
                SettingProblem(
                    "kronos.model",
                    f"{item.name} has no official public weights, so it cannot be "
                    "loaded. The README lists it as closed.",
                )
            )

        if not self.model_path.strip() and (item is None or not item.open_weights):
            problems.append(
                SettingProblem(
                    "kronos.model_path",
                    "no local model directory is set and the selected variant has no "
                    "official download source",
                )
            )

        if self.code_path.strip():
            checkout = Path(self.code_path.strip()).expanduser()
            if not (checkout / "model" / "kronos.py").exists():
                problems.append(
                    SettingProblem(
                        "kronos.code_path",
                        "the code path must be the official repository checkout; it "
                        "does not contain model/kronos.py",
                    )
                )

        if self.device not in DEVICES:
            problems.append(
                SettingProblem(
                    "kronos.device",
                    "choose " + ", ".join(DEVICES) + f" (got {self.device!r})",
                )
            )

        if self.timeframe not in TIMEFRAMES:
            problems.append(
                SettingProblem(
                    "kronos.timeframe",
                    "choose a bar timeframe AXIOM can build from the terminal: "
                    + ", ".join(TIMEFRAMES)
                    + ". Tick and sub-minute inputs are not supported by this model.",
                )
            )

        if self.volume_mode not in {"TICK_VOLUME", "ABSENT"}:
            problems.append(
                SettingProblem(
                    "kronos.volume_mode",
                    "choose TICK_VOLUME (pass MT5 tick volume, labelled) or ABSENT "
                    "(do not pass volume at all)",
                )
            )

        if not self.symbol.strip():
            problems.append(SettingProblem("kronos.symbol", "choose the symbol to forecast"))
        else:
            known = [str(symbol) for symbol in symbols]
            if known and self.symbol.strip() not in known:
                problems.append(
                    SettingProblem(
                        "kronos.symbol",
                        f"{self.symbol} is not in the provider setup's declared symbols "
                        f"({', '.join(known)})",
                    )
                )

        context_limit = item.context_length if item else 512
        if self.max_context < 16:
            problems.append(
                SettingProblem("kronos.max_context", "the context window must be at least 16")
            )
        elif self.max_context > context_limit:
            problems.append(
                SettingProblem(
                    "kronos.max_context",
                    f"{item.name if item else 'this variant'} reads at most "
                    f"{context_limit} bars; {self.max_context} would be silently "
                    "truncated by the predictor",
                )
            )

        clip = _decimal(self.clip)
        if clip is None or clip <= 0:
            problems.append(
                SettingProblem("kronos.clip", "clip must be a positive number (default 5)")
            )

        if self.lookback < MIN_LOOKBACK:
            problems.append(
                SettingProblem(
                    "kronos.lookback",
                    f"send at least {MIN_LOOKBACK} bars of context; {self.lookback} is "
                    "below AXIOM's minimum (not a Kronos limit)",
                )
            )
        elif self.lookback > self.max_context:
            problems.append(
                SettingProblem(
                    "kronos.lookback",
                    f"lookback {self.lookback} exceeds the configured context window "
                    f"{self.max_context}; lower one of them rather than relying on "
                    "truncation",
                )
            )

        if self.pred_len < MIN_HORIZON:
            problems.append(
                SettingProblem("kronos.pred_len", "the forecast horizon must be at least 1 bar")
            )
        elif self.pred_len > context_limit:
            problems.append(
                SettingProblem(
                    "kronos.pred_len",
                    f"a horizon longer than the model's {context_limit}-bar context is "
                    "not supported",
                )
            )

        temperature = _decimal(self.temperature)
        if temperature is None or temperature <= 0:
            problems.append(
                SettingProblem(
                    "kronos.temperature", "temperature must be greater than zero (default 1.0)"
                )
            )

        if self.top_k < 0:
            problems.append(
                SettingProblem("kronos.top_k", "top-k is 0 (disabled) or a positive count")
            )
        elif self.top_k > 0 and item is not None and self.top_k > 2 ** 16:
            problems.append(
                SettingProblem("kronos.top_k", "top-k is larger than the model's vocabulary")
            )

        top_p = _decimal(self.top_p)
        if top_p is None or not (Decimal(0) < top_p <= Decimal(1)):
            problems.append(
                SettingProblem(
                    "kronos.top_p", "top-p is a probability mass in (0, 1] (default 0.9)"
                )
            )

        if not (MIN_ROLLOUTS <= self.rollouts <= MAX_ROLLOUTS):
            problems.append(
                SettingProblem(
                    "kronos.rollouts",
                    f"sample between {MIN_ROLLOUTS} and {MAX_ROLLOUTS} paths; each path "
                    "is a separate predict() call, so this multiplies inference time",
                )
            )

        if self.interval_seconds < 60:
            problems.append(
                SettingProblem(
                    "kronos.interval_seconds",
                    "schedule at most once a minute; a forecast is not a tick stream",
                )
            )

        if self.seed is not None and self.seed < 0:
            problems.append(
                SettingProblem("kronos.seed", "the AXIOM-side seed must be zero or greater")
            )
        return tuple(problems)

    # -- serialisation -------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "model_path": self.model_path,
            "revision": self.revision,
            "code_path": self.code_path,
            "device": self.device,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "volume_mode": self.volume_mode,
            "max_context": self.max_context,
            "clip": str(self.clip),
            "lookback": self.lookback,
            "pred_len": self.pred_len,
            "temperature": str(self.temperature),
            "top_k": self.top_k,
            "top_p": str(self.top_p),
            "rollouts": self.rollouts,
            "interval_seconds": self.interval_seconds,
            "seed": self.seed,
            "preset": self.preset,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "KronosSettings":
        base = cls()
        get = payload.get

        def as_int(name: str, fallback: int) -> int:
            parsed = _int(get(name))
            return fallback if parsed is None else parsed

        def as_decimal(name: str, fallback: Decimal) -> Decimal:
            parsed = _decimal(get(name))
            return fallback if parsed is None else parsed

        seed = get("seed")
        parsed_seed = _int(seed)
        return cls(
            model_key=str(get("model_key") or base.model_key),
            model_path=str(get("model_path") or ""),
            revision=str(get("revision") or ""),
            code_path=str(get("code_path") or ""),
            device=str(get("device") or base.device),
            symbol=str(get("symbol") or ""),
            timeframe=str(get("timeframe") or base.timeframe),
            volume_mode=str(get("volume_mode") or base.volume_mode),
            max_context=as_int("max_context", base.max_context),
            clip=as_decimal("clip", base.clip),
            lookback=as_int("lookback", base.lookback),
            pred_len=as_int("pred_len", base.pred_len),
            temperature=as_decimal("temperature", base.temperature),
            top_k=as_int("top_k", base.top_k),
            top_p=as_decimal("top_p", base.top_p),
            rollouts=as_int("rollouts", base.rollouts),
            interval_seconds=as_int("interval_seconds", base.interval_seconds),
            seed=parsed_seed,
            preset=str(get("preset") or ""),
        )

    @classmethod
    def from_form(cls, values: Mapping[str, Any]) -> "KronosSettings":
        """Build from browser form values (strings, possibly lists)."""

        def one(name: str) -> str:
            raw = values.get(name)
            if isinstance(raw, (list, tuple)):
                raw = raw[0] if raw else ""
            return str(raw if raw is not None else "").strip()

        base = cls()

        def number(name: str, fallback: Any) -> Any:
            text = one(name)
            return fallback if text == "" else text

        return cls.from_payload(
            {
                "model_key": one("model_key") or base.model_key,
                "model_path": one("model_path"),
                "revision": one("revision"),
                "code_path": one("code_path"),
                "device": one("device") or base.device,
                "symbol": one("symbol"),
                "timeframe": one("timeframe") or base.timeframe,
                "volume_mode": one("volume_mode") or base.volume_mode,
                "max_context": number("max_context", "") or base.max_context,
                "clip": number("clip", "") or base.clip,
                "lookback": number("lookback", "") or base.lookback,
                "pred_len": number("pred_len", "") or base.pred_len,
                "temperature": number("temperature", "") or base.temperature,
                "top_k": number("top_k", "") if one("top_k") != "" else base.top_k,
                "top_p": number("top_p", "") or base.top_p,
                "rollouts": number("rollouts", "") or base.rollouts,
                "interval_seconds": number("interval_seconds", "") or base.interval_seconds,
                "seed": one("seed") or None,
            }
        )

    # -- limits shown on the page --------------------------------------

    def controls(self) -> list[dict[str, Any]]:
        """The controls, their grouping, and the model limit behind each one."""

        item = self.spec
        context_limit = item.context_length if item else 512
        return [
            _control("Basic", "model_key", "Model variant", "select", self.model_key,
                     options=[entry.key for entry in selectable_variants()],
                     help="Only variants with official public weights are listed."),
            _control("Basic", "model_path", "Model file location", "text", self.model_path,
                     help="A local directory containing the model, or blank to use the "
                          "official Hub id. Downloads are a separate, explicit action."),
            _control("Basic", "device", "Device", "select", self.device, options=list(DEVICES),
                     help="auto resolves to cuda:0, then mps, then cpu, exactly as the "
                          "predictor does."),
            _control("Basic", "symbol", "Input symbol", "text", self.symbol,
                     help="One of the symbols declared in your provider setup."),
            _control("Basic", "timeframe", "Input timeframe", "select", self.timeframe,
                     options=list(TIMEFRAMES),
                     help="Bar timeframe built from broker bid/ask data. Tick input is "
                          "not supported by this model."),
            _control("Basic", "lookback", "Context length (bars)", "number", self.lookback,
                     minimum=MIN_LOOKBACK, maximum=context_limit,
                     help=f"Bars sent to the model; the variant reads at most {context_limit}."),
            _control("Basic", "pred_len", "Forecast horizon (bars)", "number", self.pred_len,
                     minimum=MIN_HORIZON, maximum=context_limit,
                     help=f"Bars to forecast; must fit inside the model's {context_limit}-bar "
                          "context."),
            _control("Basic", "rollouts", "Forecast rollouts", "number", self.rollouts,
                     minimum=MIN_ROLLOUTS, maximum=MAX_ROLLOUTS,
                     help="Independent sampled paths. The predictor averages the paths it "
                          "is given, so AXIOM runs one predict() per rollout to keep the "
                          "spread and report bands."),
            _control("Advanced", "temperature", "Sampling temperature (T)", "number",
                     str(self.temperature), minimum="0.01", maximum="5",
                     help="predict(T=...). Higher is more diffuse."),
            _control("Advanced", "top_k", "Top-k", "number", self.top_k, minimum=0,
                     help="predict(top_k=...). 0 disables the top-k cutoff."),
            _control("Advanced", "top_p", "Top-p", "number", str(self.top_p),
                     minimum="0.01", maximum="1",
                     help="predict(top_p=...). Nucleus sampling mass."),
            _control("Advanced", "max_context", "Predictor max_context", "number",
                     self.max_context, minimum=16, maximum=context_limit,
                     help="KronosPredictor(max_context=...). AXIOM never lets it exceed "
                          "the variant's trained context."),
            _control("Advanced", "clip", "Standardisation clip", "number", str(self.clip),
                     minimum="0.1", help="KronosPredictor(clip=...). Default 5."),
            _control("Advanced", "volume_mode", "Volume mapping", "select", self.volume_mode,
                     options=["TICK_VOLUME", "ABSENT"],
                     help="MT5 gives tick volume, never exchange volume. TICK_VOLUME passes "
                          "it and labels it; ABSENT passes no volume at all."),
            _control("Advanced", "interval_seconds", "Scheduled forecast interval (s)",
                     "number", self.interval_seconds, minimum=60,
                     help="AXIOM-side scheduling. Not a model setting."),
            _control("Advanced", "seed", "AXIOM-side seed", "number",
                     "" if self.seed is None else self.seed,
                     help="Seeds the process RNG before inference. The predictor has no "
                          "seed argument, so this is best-effort and not a model control."),
            _control("Advanced", "revision", "Hub revision (pinned)", "text", self.revision,
                     help="Required before a load or download from the Hub; a moving "
                          "branch is refused."),
            _control("Advanced", "code_path", "Official code checkout", "text",
                     self.code_path,
                     help="Folder of the official Kronos repository (it contains "
                          "model/kronos.py). Blank means the model package is already "
                          "importable."),
        ]


def _control(
    group: str,
    name: str,
    label: str,
    kind: str,
    value: Any,
    *,
    options: Iterable[str] = (),
    minimum: Any = None,
    maximum: Any = None,
    help: str = "",
) -> dict[str, Any]:
    return {
        "group": group,
        "name": name,
        "label": label,
        "kind": kind,
        "value": str(value),
        "options": [str(option) for option in options],
        "min": "" if minimum is None else str(minimum),
        "max": "" if maximum is None else str(maximum),
        "help": help,
    }


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------


class PresetError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def default_preset_path(project_root: str | Path) -> Path:
    return Path(project_root) / PRESET_FILENAME


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": PRESET_SCHEMA, "presets": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PresetError("presets_unreadable", type(exc).__name__) from exc
    if not isinstance(payload, dict) or payload.get("schema") != PRESET_SCHEMA:
        raise PresetError("presets_wrong_schema")
    presets = payload.get("presets")
    if not isinstance(presets, dict):
        raise PresetError("presets_malformed")
    return payload


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix=".kronos-presets-", dir=str(path.parent), delete=False, encoding="utf-8"
    )
    try:
        handle.write(text)
        handle.close()
        temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        leftover = Path(handle.name)
        if leftover.exists():
            leftover.unlink()


def save_preset(settings: KronosSettings, name: str, path: str | Path) -> dict[str, Any]:
    label = str(name or "").strip()
    if not label:
        raise PresetError("preset_name_required")
    if len(label) > 60:
        raise PresetError("preset_name_too_long")
    target = Path(path)
    payload = _read(target)
    payload["presets"][label] = settings.to_payload()
    _write(target, payload)
    return payload


def load_preset(name: str, path: str | Path) -> KronosSettings:
    target = Path(path)
    payload = _read(target)
    stored = payload["presets"].get(str(name or "").strip())
    if not isinstance(stored, dict):
        raise PresetError("preset_not_found", str(name))
    return replace(KronosSettings.from_payload(stored), preset=str(name))


def list_presets(path: str | Path) -> list[str]:
    try:
        payload = _read(Path(path))
    except PresetError:
        return []
    return sorted(payload["presets"])


__all__ = [
    "DEVICES",
    "KronosSettings",
    "MAX_ROLLOUTS",
    "MIN_LOOKBACK",
    "MIN_ROLLOUTS",
    "PRESET_FILENAME",
    "PRESET_SCHEMA",
    "PresetError",
    "SettingProblem",
    "TIMEFRAMES",
    "default_preset_path",
    "list_presets",
    "load_preset",
    "save_preset",
]
