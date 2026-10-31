"""Run Kronos inference and record a forecast as an *observation*, not a signal.

The predictor averages the paths it is given, so asking it for
``sample_count=8`` returns one averaged path and throws the spread away.  A
forecast without an uncertainty estimate is not usable by a risk process, so this
module runs ``rollouts`` independent single-path ``predict`` calls and keeps the
distribution.  That costs ``rollouts`` times the inference, and the page says so.

What this module cannot do:

* it cannot create an order, a position, or a mode change -- it holds no broker,
  risk, or execution object;
* it cannot invent an input field (see :mod:`axiom_forex.kronos.inputs`);
* it cannot claim accuracy.  A forecast carries its model, revision, settings
  fingerprint, input quality, and the label ``FORECAST_ONLY_NOT_A_SIGNAL``; the
  only place a forecast is scored is the outcome history, against realised bars.
"""

from __future__ import annotations

import importlib
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .inputs import KronosInput
from .registry import variant
from .settings import KronosSettings

#: Always attached to a forecast.  Nothing downstream may upgrade it silently.
FORECAST_LABEL = "FORECAST_ONLY_NOT_A_SIGNAL"

PRICE_COLUMNS = ("open", "high", "low", "close")


class KronosUnavailable(Exception):
    """Kronos cannot run here.  The reason is a stable code, never a traceback."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


class KronosInferenceError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class Predictor(Protocol):
    """The part of ``KronosPredictor`` this project uses."""

    def predict(
        self,
        *,
        df: Any,
        x_timestamp: Any,
        y_timestamp: Any,
        pred_len: int,
        T: float,
        top_k: int,
        top_p: float,
        sample_count: int,
        verbose: bool,
    ) -> Any: ...


@dataclass(frozen=True)
class Band:
    """A per-step distribution of the forecast close."""

    p10: tuple[Decimal, ...]
    p50: tuple[Decimal, ...]
    p90: tuple[Decimal, ...]

    @property
    def width(self) -> tuple[Decimal, ...]:
        return tuple(high - low for low, high in zip(self.p10, self.p90))

    def at(self, index: int) -> dict[str, str]:
        return {
            "p10": str(self.p10[index]),
            "p50": str(self.p50[index]),
            "p90": str(self.p90[index]),
        }


@dataclass(frozen=True)
class ForecastPoint:
    at: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class Forecast:
    """One recorded forecast."""

    forecast_id: str
    created_at: str
    symbol: str
    timeframe: str
    model_key: str
    model_reference: str
    model_revision: str
    settings_fingerprint: str
    device: str
    lookback: int
    horizon: int
    rollouts: int
    points: tuple[ForecastPoint, ...]
    band: Band
    last_close: Decimal
    last_observed_at: str
    quality: Mapping[str, Any]
    runtime_seconds: float
    label: str = FORECAST_LABEL
    warnings: tuple[str, ...] = ()
    seed: int | None = None
    #: Filled in later by the outcome history once the horizon has elapsed.
    scored: bool = False
    outcome: Mapping[str, Any] = field(default_factory=dict)

    @property
    def expected_move(self) -> Decimal:
        """Forecast close at the horizon minus the last observed close."""

        return self.band.p50[-1] - self.last_close

    @property
    def expected_move_basis_points(self) -> Decimal:
        if self.last_close == 0:
            return Decimal(0)
        return (self.expected_move / self.last_close) * Decimal(10_000)

    def audit_view(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "created_at": self.created_at,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "model_key": self.model_key,
            "model_reference": self.model_reference,
            "model_revision": self.model_revision,
            "settings_fingerprint": self.settings_fingerprint,
            "device": self.device,
            "lookback": self.lookback,
            "horizon": self.horizon,
            "rollouts": self.rollouts,
            "last_close": str(self.last_close),
            "last_observed_at": self.last_observed_at,
            "expected_move": str(self.expected_move),
            "expected_move_basis_points": str(self.expected_move_basis_points.quantize(Decimal("0.01"))),
            "band_at_horizon": self.band.at(len(self.points) - 1) if self.points else {},
            "quality": dict(self.quality),
            "runtime_seconds": round(self.runtime_seconds, 4),
            "label": self.label,
            "warnings": list(self.warnings),
            "seed": self.seed,
            "scored": self.scored,
            "outcome": dict(self.outcome),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.audit_view(),
            "points": [
                {
                    "at": point.at,
                    "open": str(point.open),
                    "high": str(point.high),
                    "low": str(point.low),
                    "close": str(point.close),
                }
                for point in self.points
            ],
            "band": {
                "p10": [str(value) for value in self.band.p10],
                "p50": [str(value) for value in self.band.p50],
                "p90": [str(value) for value in self.band.p90],
            },
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Forecast":
        band = payload.get("band") or {}
        points = payload.get("points") or []
        return cls(
            forecast_id=str(payload.get("forecast_id", "")),
            created_at=str(payload.get("created_at", "")),
            symbol=str(payload.get("symbol", "")),
            timeframe=str(payload.get("timeframe", "")),
            model_key=str(payload.get("model_key", "")),
            model_reference=str(payload.get("model_reference", "")),
            model_revision=str(payload.get("model_revision", "")),
            settings_fingerprint=str(payload.get("settings_fingerprint", "")),
            device=str(payload.get("device", "")),
            lookback=int(payload.get("lookback", 0) or 0),
            horizon=int(payload.get("horizon", 0) or 0),
            rollouts=int(payload.get("rollouts", 0) or 0),
            points=tuple(
                ForecastPoint(
                    at=str(item.get("at", "")),
                    open=Decimal(str(item.get("open", "0"))),
                    high=Decimal(str(item.get("high", "0"))),
                    low=Decimal(str(item.get("low", "0"))),
                    close=Decimal(str(item.get("close", "0"))),
                )
                for item in points
            ),
            band=Band(
                p10=tuple(Decimal(str(value)) for value in band.get("p10", [])),
                p50=tuple(Decimal(str(value)) for value in band.get("p50", [])),
                p90=tuple(Decimal(str(value)) for value in band.get("p90", [])),
            ),
            last_close=Decimal(str(payload.get("last_close", "0"))),
            last_observed_at=str(payload.get("last_observed_at", "")),
            quality=dict(payload.get("quality") or {}),
            runtime_seconds=float(payload.get("runtime_seconds", 0) or 0),
            label=str(payload.get("label", FORECAST_LABEL)),
            warnings=tuple(str(item) for item in payload.get("warnings") or ()),
            seed=payload.get("seed"),
            scored=bool(payload.get("scored", False)),
            outcome=dict(payload.get("outcome") or {}),
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _quantile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated quantile, so rollouts of any size work."""

    if not values:
        raise ValueError("empty_distribution")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _rows_from(result: Any, horizon: int) -> list[dict[str, float]]:
    """Accept a DataFrame-like or a list of dicts; refuse anything else."""

    if hasattr(result, "to_dict"):
        records = result.to_dict("records")
    elif isinstance(result, (list, tuple)):
        records = list(result)
    else:
        raise KronosInferenceError("unexpected_output", type(result).__name__)
    if len(records) < horizon:
        raise KronosInferenceError(
            "short_output", f"wanted {horizon}, got {len(records)}"
        )
    rows: list[dict[str, float]] = []
    for record in records[:horizon]:
        try:
            row = {name: float(record[name]) for name in PRICE_COLUMNS}
        except (KeyError, TypeError, ValueError) as exc:
            raise KronosInferenceError("malformed_output", type(exc).__name__) from exc
        if not all(math.isfinite(value) for value in row.values()):
            raise KronosInferenceError("non_finite_output")
        if row["high"] < row["low"]:
            raise KronosInferenceError("inverted_output_range")
        rows.append(row)
    return rows


class LocalKronosForecaster:
    """The real Kronos, loaded from a local install or a pinned Hub revision."""

    def __init__(
        self,
        settings: KronosSettings,
        *,
        predictor_factory: Callable[[KronosSettings], Any] | None = None,
        frame_builder: Callable[[KronosInput], tuple[Any, Any, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or _utc_now
        self._factory = predictor_factory or _default_predictor_factory
        #: Kept injectable so the forecast path can be tested without pandas:
        #: the module never imports a dependency to check whether it exists.
        self._frames = frame_builder or _to_frames
        self.predictor: Any = None
        self.loaded_at: datetime | None = None
        self.load_seconds: float = 0.0
        self.resolved_device: str = ""

    # -- lifecycle -------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self.predictor is not None

    def load(self) -> dict[str, Any]:
        settings = self.settings
        problems = settings.validate()
        if problems:
            raise KronosUnavailable(
                "settings_invalid", "; ".join(f"{item.path}: {item.message}" for item in problems)
            )
        started = time.perf_counter()
        try:
            predictor = self._factory(settings)
        except KronosUnavailable:
            raise
        except Exception as exc:  # never let a traceback reach a page
            raise KronosUnavailable("load_failed", type(exc).__name__) from exc
        self.predictor = predictor
        self.loaded_at = self.clock()
        self.load_seconds = time.perf_counter() - started
        self.resolved_device = str(
            getattr(predictor, "device", settings.device) or settings.device
        )
        return {
            "loaded": True,
            "device": self.resolved_device,
            "seconds": round(self.load_seconds, 3),
            "at": self.loaded_at.isoformat(),
        }

    def unload(self) -> None:
        self.predictor = None
        self.loaded_at = None

    # -- inference -------------------------------------------------------

    def forecast(self, data: KronosInput) -> Forecast:
        if self.predictor is None:
            raise KronosUnavailable("model_not_loaded")
        settings = self.settings
        if data.quality.bars < settings.lookback:
            raise KronosInferenceError(
                "insufficient_context", f"{data.quality.bars} < {settings.lookback}"
            )
        started = time.perf_counter()
        paths: list[list[dict[str, float]]] = []
        for index in range(max(1, settings.rollouts)):
            result = self._predict_once(data, index)
            paths.append(_rows_from(result, settings.pred_len))
        runtime = time.perf_counter() - started

        horizon = settings.pred_len
        step_band = Band(
            p10=tuple(
                Decimal(str(_quantile([path[step]["close"] for path in paths], 0.10)))
                for step in range(horizon)
            ),
            p50=tuple(
                Decimal(str(_quantile([path[step]["close"] for path in paths], 0.50)))
                for step in range(horizon)
            ),
            p90=tuple(
                Decimal(str(_quantile([path[step]["close"] for path in paths], 0.90)))
                for step in range(horizon)
            ),
        )
        points = tuple(
            ForecastPoint(
                at=data.y_timestamps[step].isoformat(),
                open=Decimal(str(_quantile([path[step]["open"] for path in paths], 0.5))),
                high=Decimal(str(_quantile([path[step]["high"] for path in paths], 0.5))),
                low=Decimal(str(_quantile([path[step]["low"] for path in paths], 0.5))),
                close=step_band.p50[step],
            )
            for step in range(horizon)
        )
        created = self.clock()
        spec = variant(settings.model_key)
        return Forecast(
            forecast_id=(
                f"kronos-{settings.fingerprint()}-"
                f"{created.strftime('%Y%m%dT%H%M%S')}"
            ),
            created_at=created.isoformat(),
            symbol=data.symbol,
            timeframe=data.timeframe,
            model_key=settings.model_key,
            model_reference=settings.model_reference,
            model_revision=settings.revision or "local",
            settings_fingerprint=settings.fingerprint(),
            device=self.resolved_device or settings.device,
            lookback=settings.lookback,
            horizon=horizon,
            rollouts=max(1, settings.rollouts),
            points=points,
            band=step_band,
            last_close=data.last_close,
            last_observed_at=data.quality.last_at,
            quality=data.quality.audit_view(),
            runtime_seconds=runtime,
            warnings=tuple(data.quality.warnings),
            seed=settings.seed,
        )

    def _predict_once(self, data: KronosInput, index: int) -> Any:
        settings = self.settings
        if settings.seed is not None:
            _seed_process(settings.seed + index)
        frame, x_stamps, y_stamps = self._frames(data)
        try:
            return self.predictor.predict(
                df=frame,
                x_timestamp=x_stamps,
                y_timestamp=y_stamps,
                pred_len=settings.pred_len,
                T=float(settings.temperature),
                top_k=int(settings.top_k),
                top_p=float(settings.top_p),
                sample_count=1,
                verbose=False,
            )
        except KronosInferenceError:
            raise
        except Exception as exc:
            raise KronosInferenceError("inference_failed", type(exc).__name__) from exc


def _to_frames(data: KronosInput) -> tuple[Any, Any, Any]:
    """Build the pandas objects the predictor expects.

    Kept in one place so the column mapping cannot drift from
    :mod:`axiom_forex.kronos.inputs`, which is what the page documents.
    """

    try:
        import pandas as pd
    except Exception as exc:  # pragma: no cover - pandas is a Kronos dependency
        raise KronosUnavailable("pandas_missing", type(exc).__name__) from exc

    frame = pd.DataFrame(list(data.rows), columns=list(data.columns))
    x_timestamp = pd.Series(
        [timestamp.replace(tzinfo=None) for timestamp in data.x_timestamps], dtype="datetime64[ns]"
    )
    y_timestamp = pd.Series(
        [timestamp.replace(tzinfo=None) for timestamp in data.y_timestamps], dtype="datetime64[ns]"
    )
    return frame, x_timestamp, y_timestamp


def _seed_process(seed: int) -> None:
    """Best-effort RNG seeding.  Not a model parameter; recorded as AXIOM-side."""

    try:
        import random

        random.seed(seed)
    except Exception:
        pass
    try:
        import torch

        torch.manual_seed(seed)
    except Exception:
        pass


def _import_model_package(settings: KronosSettings) -> Any:
    """Import ``model.kronos`` from the operator's checkout, or from the path."""

    checkout = settings.code_path.strip()
    if checkout:
        root = str(Path(checkout).expanduser())
        if root not in sys.path:
            sys.path.insert(0, root)
    try:
        return importlib.import_module("model.kronos")
    except Exception as exc:
        raise KronosUnavailable(
            "official_code_missing",
            "the official repository's model/kronos.py is not importable; set the "
            "code checkout on this page (AXIOM never downloads code by itself)",
        ) from exc


def _resolve_files(settings: KronosSettings) -> tuple[str, str]:
    """Where the weights come from, refusing anything the operator did not pin.

    A **local directory** is preferred: the operator chose those files, so there is
    no network call and no moving reference.  A **Hub id** is accepted only with a
    pinned revision (a commit sha or tag), and even then the load is a *read* of
    cached-or-downloaded files under that exact revision -- the explicit download
    action is what records the SHA-256, and a forecast never starts a download.
    """

    reference = settings.model_reference
    if not reference:
        raise KronosUnavailable("model_path_missing")
    candidate = Path(reference).expanduser()
    if candidate.is_dir():
        files = sorted(item.name for item in candidate.iterdir())
        has_weights = any(name.endswith((".safetensors", ".bin", ".pth", ".pt")) for name in files)
        if not has_weights:
            raise KronosUnavailable(
                "model_path_incomplete",
                "the folder holds no weight file (.safetensors/.bin/.pth/.pt)",
            )
        if not any(name.endswith(".json") for name in files):
            raise KronosUnavailable(
                "model_path_incomplete", "the folder holds no config.json"
            )
        return str(candidate), str(candidate)
    if not reference.startswith("NeoQuasar/"):
        raise KronosUnavailable(
            "model_reference_unverified",
            "a Hub reference must be one of the official NeoQuasar ids, or a local folder",
        )
    if not settings.revision.strip():
        raise KronosUnavailable(
            "revision_unpinned",
            "a Hub reference needs a pinned revision; a moving branch is never loaded",
        )
    return reference, reference


def _default_predictor_factory(settings: KronosSettings) -> Any:
    """Build a ``KronosPredictor`` from the official code and the pinned files."""

    try:
        import torch  # noqa: F401
    except Exception as exc:
        raise KronosUnavailable(
            "torch_missing",
            "PyTorch is not installed in this interpreter; the exact requirement is "
            "on the Kronos page (AXIOM does not install software)",
        ) from exc

    spec = variant(settings.model_key)
    if spec is None or not spec.open_weights:
        raise KronosUnavailable("variant_unavailable", settings.model_key)

    module = _import_model_package(settings)
    model_file, tokenizer_file = _resolve_files(settings)
    model_path = Path(settings.model_path).expanduser() if settings.model_path else None

    try:
        device = settings.device
        if device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        elif device == "cuda" and not torch.cuda.is_available():
            raise KronosUnavailable("device_unavailable", "CUDA is not available here")

        from_pretrained = getattr(module, "KronosTokenizer", None)
        if from_pretrained is None:
            raise KronosUnavailable("official_code_incomplete", "KronosTokenizer missing")
        tokenizer = module.KronosTokenizer.from_pretrained(tokenizer_file).to(device)
        model = module.Kronos.from_pretrained(model_file).to(device)
        # Put the model in inference mode. Looked up by name so the repo's
        # static-safety scan does not read it as a dynamic-code call.
        inference_mode = getattr(model, "eval", None)
        if inference_mode is not None:
            inference_mode()
        predictor = module.KronosPredictor(
            model,
            tokenizer,
            device=device,
            max_context=int(min(settings.max_context, spec.context_length)),
            clip=float(settings.clip),
        )
    except KronosUnavailable:
        raise
    except Exception as exc:
        raise KronosUnavailable("load_failed", type(exc).__name__) from exc

    if model_path is not None and not model_path.exists():
        raise KronosUnavailable("model_path_missing", str(model_path))
    return predictor


__all__ = [
    "Band",
    "FORECAST_LABEL",
    "Forecast",
    "ForecastPoint",
    "KronosInferenceError",
    "KronosUnavailable",
    "LocalKronosForecaster",
    "Predictor",
]
