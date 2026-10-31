"""Whether Kronos can run here, roughly what it will cost, and its state machine.

Three separate questions, deliberately kept apart:

**Can it run?**  Answered by looking for the packages the official
``requirements.txt`` names.  The probe uses :func:`importlib.util.find_spec`, so it
does not import torch to find out whether torch exists -- importing torch costs
seconds and, on some builds, a CUDA context.  A missing package is reported with
the exact requirement line and the instruction to install it manually: AXIOM never
installs software.

**What will it cost?**  A *heuristic*, not a measurement.  The weight bytes come
from the parameter count in :mod:`axiom_forex.kronos.registry`, which is a count
published by the project, times 4 bytes per parameter (fp32).  Activations are not
estimated from the model's internals; the page reports the context window and says
plainly that the figure grows with it.  Load time is a range, never a promise.

**What state is it in?**  ``NOT_LOADED`` -> ``LOADING`` -> ``READY``, or
``FAILED`` with a curated reason, or ``STOPPED`` after an explicit stop.  A failure
is sticky until the operator changes a setting or retries: nothing here retries by
itself, and nothing here downloads, installs, or writes a credential.
"""

from __future__ import annotations

import importlib.util
import platform
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

from .registry import (
    RUNTIME_REQUIREMENTS,
    SOURCE_COMMIT,
    SOURCE_REPO,
    selectable_variants,
    variant,
)
from .settings import KronosSettings, TIMEFRAMES

#: Lifecycle states.  ``STOPPED`` is not ``FAILED``: the operator did it on purpose.
NOT_LOADED = "NOT_LOADED"
LOADING = "LOADING"
READY = "READY"
FAILED = "FAILED"
STOPPED = "STOPPED"

#: Import name -> the requirement line it satisfies.  ``einops`` etc. are mapped
#: exactly as the package names them, so a report can name the requirement the
#: operator must install.
_PACKAGE_IMPORTS: dict[str, str] = {
    "numpy": "numpy",
    "pandas": "pandas==2.2.2",
    "torch": "torch>=2.0.0",
    "einops": "einops==0.8.1",
    "huggingface_hub": "huggingface_hub==0.33.1",
    "matplotlib": "matplotlib==3.9.3",
    "tqdm": "tqdm==4.67.1",
    "safetensors": "safetensors==0.6.2",
}

#: The subset that inference actually needs.  ``matplotlib`` is in the project's
#: requirements for its own plots and is not needed to forecast, so it is reported
#: as optional rather than blocking.
REQUIRED_FOR_INFERENCE = ("torch", "pandas", "numpy", "einops", "safetensors", "huggingface_hub")
OPTIONAL_FOR_INFERENCE = ("matplotlib", "tqdm")

#: Nothing in this module retries by itself; a failure waits for the operator.
NO_AUTOMATIC_RETRY = "no_automatic_retry"

#: Settings that change *what is read and forecast*, not what is loaded.  They can
#: be changed on a loaded model without reloading the weights: the model is the
#: same object, and only the request around it moves.
READ_SIDE_FIELDS: tuple[str, ...] = (
    "symbol",
    "timeframe",
    "volume_mode",
    "pred_len",
    "temperature",
    "top_k",
    "top_p",
    "rollouts",
    "interval_seconds",
    "seed",
)

#: Settings that are baked into the loaded predictor.  Changing one of these is a
#: reload, and AXIOM says so instead of pretending it took effect.
STRUCTURAL_FIELDS: tuple[str, ...] = (
    "model_key",
    "model_path",
    "tokenizer_path",
    "revision",
    "code_path",
    "device",
    "max_context",
    "clip",
)

#: Bytes per parameter, for the estimate only.
BYTES_PER_PARAMETER = 4


@dataclass(frozen=True)
class PackageFact:
    import_name: str
    requirement: str
    present: bool
    version: str
    required: bool

    def audit_view(self) -> dict[str, Any]:
        return {
            "import_name": self.import_name,
            "requirement": self.requirement,
            "present": self.present,
            "version": self.version,
            "required": self.required,
        }


@dataclass(frozen=True)
class Availability:
    """What this interpreter has, checked without installing or importing torch."""

    python: str
    platform: str
    packages: tuple[PackageFact, ...]
    torch_present: bool
    torch_version: str = ""
    cuda_available: bool | None = None
    cuda_devices: int = 0
    device_names: tuple[str, ...] = ()
    note: str = ""

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(item.requirement for item in self.packages if item.required and not item.present)

    @property
    def can_run(self) -> bool:
        return not self.missing

    def audit_view(self) -> dict[str, Any]:
        return {
            "python": self.python,
            "platform": self.platform,
            "packages": [item.audit_view() for item in self.packages],
            "missing": list(self.missing),
            "torch_present": self.torch_present,
            "torch_version": self.torch_version,
            "cuda_available": self.cuda_available,
            "cuda_devices": self.cuda_devices,
            "device_names": list(self.device_names),
            "can_run": self.can_run,
            "note": self.note,
        }


def _version(module: Any) -> str:
    for attribute in ("__version__", "version"):
        value = getattr(module, attribute, None)
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


def probe_availability(*, deep: bool = False) -> Availability:
    """Which dependencies are here.

    ``deep=True`` imports torch to ask about CUDA.  That is slow and touches the
    GPU, so it only happens when the operator asks for the hardware estimate -- a
    page render never does it.
    """

    packages: list[PackageFact] = []
    for import_name, requirement in _PACKAGE_IMPORTS.items():
        present = False
        version = ""
        try:
            present = importlib.util.find_spec(import_name) is not None
        except (ImportError, ValueError):
            present = False
        if present and deep:
            try:
                version = _version(importlib.import_module(import_name))
            except Exception:
                version = ""
        packages.append(
            PackageFact(
                import_name=import_name,
                requirement=requirement,
                present=present,
                version=version,
                required=import_name in REQUIRED_FOR_INFERENCE,
            )
        )

    torch_fact = next((item for item in packages if item.import_name == "torch"), None)
    torch_present = bool(torch_fact and torch_fact.present)
    cuda_available: bool | None = None
    cuda_devices = 0
    device_names: tuple[str, ...] = ()
    note = ""
    if deep and torch_present:
        try:
            import torch

            cuda_available = bool(torch.cuda.is_available())
            if cuda_available:
                cuda_devices = int(torch.cuda.device_count())
                device_names = tuple(
                    str(torch.cuda.get_device_name(index)) for index in range(cuda_devices)
                )
        except Exception:
            cuda_available = None
            note = "torch is installed but could not be queried for CUDA"
    elif deep and not torch_present:
        note = "torch is not installed, so no device can be queried"

    return Availability(
        python=platform.python_version(),
        platform=platform.platform(),
        packages=tuple(packages),
        torch_present=torch_present,
        torch_version=torch_fact.version if torch_fact else "",
        cuda_available=cuda_available,
        cuda_devices=cuda_devices,
        device_names=device_names,
        note=note,
    )


# ---------------------------------------------------------------------------
# cost estimate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    """A range, not a measurement.  Never rendered as a promise."""

    parameters: int
    weights_megabytes: float
    context_bars: int
    horizon_bars: int
    rollouts: int
    past_tokens: int
    future_tokens: int
    notes: tuple[str, ...]

    def audit_view(self) -> dict[str, Any]:
        return {
            "parameters": self.parameters,
            "weights_megabytes": round(self.weights_megabytes, 1),
            "context_bars": self.context_bars,
            "horizon_bars": self.horizon_bars,
            "rollouts": self.rollouts,
            "past_tokens": self.past_tokens,
            "future_tokens": self.future_tokens,
            "notes": list(self.notes),
        }


def estimate(settings: KronosSettings) -> Estimate:
    spec = variant(settings.model_key)
    parameters = spec.parameters if spec else 0
    context = int(min(settings.lookback, settings.max_context))
    notes = [
        "Weight size assumes fp32 (4 bytes per parameter). A checkpoint stored in "
        "bfloat16 is about half of this; the file's own size on disk is the fact, "
        "this is the estimate.",
        "Activation memory is not estimated: it depends on the attention "
        "implementation and grows with the context window. Watch the first load's "
        "memory on this machine before scheduling long runs.",
        f"Load time is dominated by reading the checkpoint from disk. Network "
        f"transfer is never included here because AXIOM does not download by itself.",
    ]
    if settings.rollouts > 1:
        notes.append(
            f"{settings.rollouts} rollouts means {settings.rollouts} sequential "
            "predict() calls; wall-clock inference time multiplies accordingly."
        )
    if spec is not None and not spec.open_weights:
        notes.append(f"{spec.name} has no public weights in the official zoo.")
    return Estimate(
        parameters=parameters,
        weights_megabytes=(parameters * BYTES_PER_PARAMETER) / (1024 * 1024),
        context_bars=context,
        horizon_bars=settings.pred_len,
        rollouts=max(1, settings.rollouts),
        # The tokenizer works one token per bar for the past, and one per forecast
        # bar. Both are the operative counts for the context window.
        past_tokens=context,
        future_tokens=settings.pred_len,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------


@dataclass
class RuntimeState:
    state: str = NOT_LOADED
    reason: str = ""
    detail: str = ""
    device: str = ""
    since: str = ""
    seconds: float = 0.0
    forecasts: int = 0
    failures: int = 0
    last_forecast_at: str = ""
    last_error: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "device": self.device,
            "since": self.since,
            "seconds": round(self.seconds, 3),
            "forecasts": self.forecasts,
            "failures": self.failures,
            "last_forecast_at": self.last_forecast_at,
            "last_error": self.last_error,
            "loaded": self.state == READY,
        }


class KronosRuntime:
    """Owns the loaded model and the scheduled loop.  One instance per workstation.

    It never creates an order, never changes an execution mode, and never writes a
    risk setting: the only sink it has is a *forecast* callback, which the caller
    chooses.  The default sink discards the forecast after recording it.
    """

    def __init__(
        self,
        *,
        forecaster_factory: Callable[[KronosSettings], Any],
        inputs_provider: Callable[[KronosSettings], Any] | None = None,
        on_forecast: Callable[[Any], None] | None = None,
        clock: Callable[[], Any] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        from datetime import datetime, timezone

        self._factory = forecaster_factory
        self._inputs = inputs_provider
        self._sink = on_forecast
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._log = logger or (lambda message: None)
        self.settings: KronosSettings | None = None
        self.forecaster: Any = None
        self.state = RuntimeState()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    # -- state ----------------------------------------------------------

    def view(self) -> dict[str, Any]:
        with self._lock:
            return self.state.audit_view()

    def _set(self, state: str, **fields: Any) -> None:
        with self._lock:
            self.state.state = state
            self.state.since = self._clock().isoformat()
            for name, value in fields.items():
                setattr(self.state, name, value)

    # -- loading --------------------------------------------------------

    def load(self, settings: KronosSettings) -> dict[str, Any]:
        """Load once.  A failure leaves ``FAILED`` with a curated reason."""

        with self._lock:
            if self.state.state == READY:
                return {
                    "loaded": True,
                    "reason": "already_loaded",
                    "state": self.state.audit_view(),
                    "retry": NO_AUTOMATIC_RETRY,
                }
            problems = settings.validate()
            if problems:
                self._set(
                    FAILED,
                    reason="settings_invalid",
                    detail="; ".join(f"{item.path}: {item.message}" for item in problems),
                )
                return self._failure()
            self.settings = settings
            self._set(LOADING, reason="", detail="")
        started = time.perf_counter()
        try:
            forecaster = self._factory(settings)
            report = forecaster.load() if hasattr(forecaster, "load") else {}
        except Exception as exc:  # curated: never a traceback on the page
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            detail = str(getattr(exc, "detail", "") or "")
            self._set(FAILED, reason=reason, detail=detail, seconds=time.perf_counter() - started)
            self._log(f"kronos load failed: {reason}")
            return self._failure()
        with self._lock:
            self.forecaster = forecaster
            self._set(
                READY,
                reason="",
                detail="",
                device=str(report.get("device", "") if isinstance(report, Mapping) else ""),
                seconds=time.perf_counter() - started,
                last_error="",
            )
            return {"loaded": True, "reason": "", "state": self.state.audit_view(), "retry": NO_AUTOMATIC_RETRY}

    def _failure(self) -> dict[str, Any]:
        with self._lock:
            return {
                "loaded": False,
                "reason": self.state.reason,
                "detail": self.state.detail,
                "state": self.state.audit_view(),
                "retry": NO_AUTOMATIC_RETRY,
            }

    def stop(self, *, reason: str = "operator_stopped") -> dict[str, Any]:
        """Stop the loop and drop the model.  Idempotent, and always deliberate."""

        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None
        with self._lock:
            forecaster = self.forecaster
            self.forecaster = None
            unloaded = False
            if forecaster is not None and hasattr(forecaster, "unload"):
                try:
                    forecaster.unload()
                    unloaded = True
                except Exception:
                    unloaded = False
            self._set(STOPPED, reason=reason, detail="", seconds=0.0)
            view = self.state.audit_view()
        view["unloaded"] = unloaded
        return view

    # -- changing what is read, without reloading ------------------------

    def update_settings(self, settings: KronosSettings) -> dict[str, Any]:
        """Apply read-side changes to the loaded model; name the ones it cannot take.

        Nothing is loaded, reloaded, connected, or traded here.  A structural change
        (a different checkpoint, device, context window) is reported as needing a
        reload rather than being silently ignored.
        """

        with self._lock:
            current = self.settings
            if current is None:
                return {"applied": [], "reload_required": []}
            applied = [
                name
                for name in READ_SIDE_FIELDS
                if getattr(current, name, None) != getattr(settings, name, None)
            ]
            reload_required = [
                name
                for name in STRUCTURAL_FIELDS
                if getattr(current, name, None) != getattr(settings, name, None)
            ]
            if applied:
                merged = replace(current, **{name: getattr(settings, name) for name in READ_SIDE_FIELDS})
                self.settings = merged
                forecaster = self.forecaster
                if forecaster is not None and hasattr(forecaster, "settings"):
                    forecaster.settings = merged
            return {"applied": applied, "reload_required": reload_required}

    # -- one forecast ---------------------------------------------------

    def forecast_once(self) -> dict[str, Any]:
        """Run one forecast and hand it to the sink.  Never creates an order."""

        with self._lock:
            forecaster = self.forecaster
            settings = self.settings
        if forecaster is None or settings is None:
            return {"ok": False, "reason": "model_not_loaded"}
        if self._inputs is None:
            return {"ok": False, "reason": "no_input_source"}
        try:
            data = self._inputs(settings)
            result = forecaster.forecast(data)
        except Exception as exc:
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            detail = str(getattr(exc, "detail", "") or "")
            with self._lock:
                self.state.failures += 1
                self.state.last_error = reason
            self._log(f"kronos forecast failed: {reason}")
            return {"ok": False, "reason": reason, "detail": detail}
        with self._lock:
            self.state.forecasts += 1
            self.state.last_forecast_at = self._clock().isoformat()
            self.state.last_error = ""
        if self._sink is not None:
            try:
                self._sink(result)
            except Exception as exc:
                self._log(f"forecast sink failed: {type(exc).__name__}")
        return {"ok": True, "forecast": result}

    # -- scheduled ------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self, *, interval_seconds: int | None = None) -> dict[str, Any]:
        """Start scheduled forecasting.  Refuses unless the model is READY."""

        with self._lock:
            if self.state.state != READY or self.forecaster is None:
                return {"started": False, "reason": "model_not_loaded"}
            if self.running:
                return {"started": False, "reason": "already_running"}
            seconds = int(interval_seconds or (self.settings.interval_seconds if self.settings else 900))
            if seconds < 60:
                return {"started": False, "reason": "interval_too_short"}
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, args=(seconds,), name="axiom-kronos-scheduler", daemon=True
            )
            self._thread.start()
        self._log(f"kronos scheduled forecasting started every {seconds}s")
        return {"started": True, "reason": "", "interval_seconds": seconds}

    def stop_schedule(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        return {"stopped": True}

    def _loop(self, seconds: int) -> None:
        # The first forecast happens immediately: waiting a whole interval before
        # the first result makes a start look broken.
        while not self._stop.is_set():
            self.forecast_once()
            if self._stop.wait(seconds):
                return


__all__ = [
    "Availability",
    "Estimate",
    "FAILED",
    "LOADING",
    "NOT_LOADED",
    "NO_AUTOMATIC_RETRY",
    "OPTIONAL_FOR_INFERENCE",
    "READ_SIDE_FIELDS",
    "PackageFact",
    "READY",
    "REQUIRED_FOR_INFERENCE",
    "RUNTIME_REQUIREMENTS",
    "RuntimeState",
    "SOURCE_COMMIT",
    "SOURCE_REPO",
    "STOPPED",
    "STRUCTURAL_FIELDS",
    "KronosRuntime",
    "TIMEFRAMES",
    "estimate",
    "probe_availability",
    "selectable_variants",
]
