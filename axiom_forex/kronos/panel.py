"""The state behind the ``/kronos`` page: settings, runtime, history, benchmark.

The page is deliberately separate from Provider setup and from the strategy pages.
It can change what a *forecast* looks like and nothing else: it holds no risk
limit, no execution mode, no plan, and no broker write surface.  A forecast
produced here is labelled ``FORECAST_ONLY_NOT_A_SIGNAL`` and the sink that stores
it is the only thing it touches.

Every action returns a plain dict so the HTTP layer can turn it into a status and
an operator-facing sentence without knowing anything about Kronos:

``{"ok": bool, "message": str, "reason": str, "path": str}``

The one exception is the install: an explicit, cancellable download of the
official sources into AXIOM's own folder, started only when the operator presses
Install.  Nothing here downloads on import, on startup, or on a page render, and
nothing here can trade.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .benchmark import KronosModel, evaluate, standard_baselines
from .forecast import FORECAST_LABEL, Forecast, LocalKronosForecaster
from .history import ForecastStore, reconcile
from .install import (
    INSTALL_SCHEMA,
    KronosInstaller,
    managed_root,
    recommended_assets,
)
from .inputs import InputRefused, build_input, describe_mapping
from .registry import (
    CODE_LICENCE,
    RECOMMENDED_VARIANT,
    CODE_LICENCE_COPYRIGHT,
    PAPER_URL,
    RUNTIME_REQUIREMENTS,
    SOURCE_COMMIT_URL,
    SOURCE_REPO,
    UNSUPPORTED_SETTINGS,
    WEIGHTS_HOST,
    WEIGHTS_LICENCE_STATE,
    selectable_variants,
)
from .runtime import KronosRuntime, estimate, probe_availability
from .settings import (
    TIMEFRAMES,
    KronosSettings,
    PresetError,
    default_preset_path,
    list_presets,
    load_preset,
    save_preset,
)

SETTINGS_SCHEMA = "axiom-kronos-settings-v1"
SETTINGS_FILENAME = "kronos_settings.local.json"
HISTORY_FILENAME = "kronos_forecasts.sqlite3"
BENCHMARK_FILENAME = "kronos_benchmark.local.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _settings_payload(form: Mapping[str, Any] | None) -> dict[str, Any]:
    """The form fields, minus the CSRF nonce.

    A "Load model" form carries no settings at all: it loads what is saved. Without
    this, an empty form would parse as a blank configuration and be refused.
    """

    if not form:
        return {}
    return {str(name): value for name, value in form.items() if str(name) != "nonce"}


def _result(ok: bool, message: str, *, reason: str = "", path: str = "/kronos") -> dict[str, Any]:
    return {"ok": ok, "message": message, "reason": reason, "path": path}


@dataclass
class KronosPanel:
    """Everything the page needs, with no broker or risk object in reach."""

    root: Path
    #: A callable ``(symbol, timeframe, count) -> bars`` supplied by the caller.
    #: ``None`` means this deployment has no bar source, and the page says so
    #: instead of forecasting on nothing.
    bars_provider: Callable[[str, str, int], Sequence[Any]] | None = None
    forecaster_factory: Callable[[KronosSettings], Any] | None = None
    clock: Callable[[], datetime] = _utc_now
    logger: Callable[[str], None] | None = None

    _settings: KronosSettings | None = None
    _runtime: KronosRuntime | None = None
    _store: ForecastStore | None = None
    _last: dict[str, Any] = field(default_factory=dict)
    _last_payload: dict[str, Any] = field(default_factory=dict)
    _weights: dict[str, Any] = field(default_factory=dict)
    _last_benchmark: dict[str, Any] = field(default_factory=dict)
    _notice: dict[str, Any] = field(default_factory=dict)
    #: The install flow.  Injected by tests; built on first use otherwise.
    install_system: Any = None
    #: The symbols Account & symbols verified and the operator selected.  Read-only
    #: here: this page can pick one of them, never add to them.
    _symbols: tuple[str, ...] = ()

    # -- paths ----------------------------------------------------------

    @property
    def settings_path(self) -> Path:
        return Path(self.root) / SETTINGS_FILENAME

    @property
    def presets_path(self) -> Path:
        return default_preset_path(self.root)

    @property
    def history_path(self) -> Path:
        return Path(self.root) / HISTORY_FILENAME

    @property
    def benchmark_path(self) -> Path:
        return Path(self.root) / BENCHMARK_FILENAME

    # -- settings -------------------------------------------------------

    def apply_installed_paths(self) -> bool:
        """Remember the paths the install recorded, once, without overwriting yours.

        Only blank fields are filled.  A path the operator typed under Advanced
        settings stays exactly as typed.
        """

        recorded = self.installer().paths(self.settings().model_key)
        if not recorded:
            return False
        settings = self.settings()
        updates = {
            name: recorded[name]
            for name in ("model_path", "tokenizer_path", "code_path", "revision")
            if recorded.get(name) and not str(getattr(settings, name) or "").strip()
        }
        if not updates:
            return False
        settings = replace(settings, **updates)
        self._settings = settings
        self._write_settings(settings)
        return True

    def settings_for(self, symbols: Iterable[str] = ()) -> KronosSettings:
        """The settings a page should show: installed paths, and a first symbol.

        The first verified symbol selected on Account & symbols becomes the default
        input on the first run, and is remembered.  It is never a substitute for a
        symbol the operator typed: only a blank value is filled.
        """

        self.apply_installed_paths()
        settings = self.settings()
        chosen = [str(item) for item in symbols if str(item).strip()]
        if not settings.symbol.strip() and chosen:
            settings = replace(settings, symbol=chosen[0])
            self._settings = settings
            self._write_settings(settings)
        return settings

    def settings(self) -> KronosSettings:
        if self._settings is None:
            self._settings = self._read_settings()
        return self._settings

    def _read_settings(self) -> KronosSettings:
        path = self.settings_path
        if not path.exists():
            return KronosSettings()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return KronosSettings()
        if not isinstance(payload, dict) or payload.get("schema") != SETTINGS_SCHEMA:
            return KronosSettings()
        stored = payload.get("settings")
        if not isinstance(stored, dict):
            return KronosSettings()
        try:
            return KronosSettings.from_payload(stored)
        except Exception:
            return KronosSettings()

    def _write_settings(self, settings: KronosSettings) -> None:
        path = self.settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": SETTINGS_SCHEMA, "settings": settings.to_payload()}
        handle = tempfile.NamedTemporaryFile(
            mode="w", prefix=".kronos-settings-", dir=str(path.parent), delete=False, encoding="utf-8"
        )
        try:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.close()
            Path(handle.name).replace(path)
        finally:
            leftover = Path(handle.name)
            if leftover.exists():
                leftover.unlink()

    # -- collaborators --------------------------------------------------

    def store(self) -> ForecastStore:
        if self._store is None:
            self._store = ForecastStore(self.history_path, clock=self.clock)
        return self._store

    def runtime(self) -> KronosRuntime:
        if self._runtime is None:
            self._runtime = KronosRuntime(
                forecaster_factory=self.forecaster_factory or self._default_factory,
                inputs_provider=self.inputs_for,
                on_forecast=self._record,
                clock=self.clock,
                logger=self.logger,
            )
        return self._runtime

    def installer(self) -> KronosInstaller:
        """The one install flow, built on first use.  Nothing runs on construction."""

        if self.install_system is None:
            self.install_system = KronosInstaller(Path(self.root), logger=self.logger)
        return self.install_system

    def _default_factory(self, settings: KronosSettings) -> Any:
        return LocalKronosForecaster(settings, clock=self.clock)

    def inputs_for(self, settings: KronosSettings):
        """Build the predictor's input from this deployment's bars."""

        if self.bars_provider is None:
            raise InputRefused(
                "no_bar_source",
                "this workstation was not given a bar source, so there is nothing to forecast",
            )
        timeframe = settings.timeframe
        seconds = TIMEFRAMES.get(timeframe, 0)
        if seconds <= 0:
            raise InputRefused("unsupported_timeframe", timeframe)
        wanted = int(settings.lookback) + 8
        bars = list(self.bars_provider(settings.symbol, timeframe, wanted))
        return build_input(
            bars,
            symbol=settings.symbol,
            timeframe=timeframe,
            lookback=int(settings.lookback),
            horizon=int(settings.pred_len),
            bar_seconds=seconds,
            volume_mode=settings.volume_mode,
        )

    def _record(self, forecast: Forecast) -> None:
        self.store().record(forecast)
        self._last = forecast.audit_view()
        self._last_payload = forecast.to_payload()

    def inspect_weights(self, form: Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        """Fingerprint the model files the operator placed on this machine.

        AXIOM does not download them.  This action reads the folder, hashes each
        file, and records the digests so the operator can compare them with the
        publisher's own values.  It is labelled ``UNVERIFIED_BY_AXIOM`` because
        AXIOM has nothing upstream to compare against -- claiming otherwise would
        be the whole problem.
        """

        payload = form if isinstance(form, Mapping) else {}
        raw = payload.get("model_path")
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else ""
        reference = str(raw or self.settings().model_path or "").strip()
        if not reference:
            return _result(False, "Set the model file location first.", reason="model_path_missing")
        folder = Path(reference).expanduser()
        if not folder.is_dir():
            return _result(
                False,
                "That is not a local folder. A Hub reference is not inspected here: AXIOM "
                "downloads nothing, so there is nothing of its own to hash.",
                reason="not_a_local_folder",
            )
        import hashlib

        rows: list[dict[str, Any]] = []
        total = 0
        for item in sorted(folder.rglob("*")):
            if not item.is_file():
                continue
            digest = hashlib.sha256()
            size = 0
            try:
                with item.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
            except OSError:
                continue
            total += size
            rows.append(
                {
                    "path": str(item.relative_to(folder)),
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )
        if not rows:
            return _result(False, "That folder holds no files.", reason="empty_folder")
        record = {
            "folder": str(folder),
            "files": rows,
            "total_bytes": total,
            "state": "UNVERIFIED_BY_AXIOM",
            "checked_at": self.clock().isoformat(),
            "note": (
                "Digests computed locally. AXIOM downloaded nothing and compared "
                "nothing against the publisher: compare these values with the "
                "official model card yourself before relying on the files."
            ),
        }
        self._weights = record
        try:
            path = Path(self.root) / "kronos_weights.local.json"
            path.write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
        return _result(
            True,
            f"Hashed {len(rows)} file(s), {total} bytes. State stays "
            "UNVERIFIED_BY_AXIOM until you compare them with the publisher's values.",
        )

    # -- install --------------------------------------------------------

    def install_check(self) -> dict[str, Any]:
        """Read the official sources' metadata.  Downloads nothing."""

        outcome = self.installer().check(self.settings().model_key)
        return self._install_outcome(outcome)

    def install_start(self) -> dict[str, Any]:
        """Start the one install the operator asked for, after a check."""

        outcome = self.installer().install(self.settings().model_key)
        return self._install_outcome(outcome)

    def install_cancel(self) -> dict[str, Any]:
        outcome = self.installer().cancel()
        return self._install_outcome(outcome)

    def install_cleanup(self) -> dict[str, Any]:
        """Remove the managed folder, and forget any path that pointed into it."""

        root = managed_root(self.root)
        outcome = self.installer().cleanup(self.settings().model_key)
        if not outcome.get("ok"):
            return self._install_outcome(outcome)
        settings = self.settings()
        updates: dict[str, str] = {}
        for name in ("model_path", "tokenizer_path", "code_path"):
            value = str(getattr(settings, name) or "").strip()
            if not value:
                continue
            candidate = Path(value)
            if candidate == root or root in candidate.parents:
                updates[name] = ""
        if updates:
            settings = replace(settings, **updates)
            self._settings = settings
            self._write_settings(settings)
        # A loaded model whose files just disappeared must not keep reporting READY.
        if self.runtime().view().get("state") == "READY":
            self.runtime().stop(reason="install_removed")
        return self._install_outcome(outcome)

    def _install_outcome(self, outcome: Mapping[str, Any]) -> dict[str, Any]:
        if not outcome.get("ok"):
            return _result(
                False,
                str(outcome.get("message") or "The install action did not run."),
                reason=str(outcome.get("reason") or ""),
            )
        self.apply_installed_paths()
        return _result(True, str(outcome.get("message") or "The install action ran."))

    # -- actions --------------------------------------------------------

    def save(self, form: Mapping[str, Any]) -> dict[str, Any]:
        settings = KronosSettings.from_form(form)
        problems = settings.validate()
        if problems:
            return _result(
                False,
                "Nothing was saved: "
                + "; ".join(f"{item.path}: {item.message}" for item in problems),
                reason="settings_invalid",
            )
        self._settings = settings
        self._write_settings(settings)
        return _result(
            True,
            "Settings saved locally. Nothing was loaded, downloaded, or started.",
        )

    def load(self, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = _settings_payload(form)
        self.apply_installed_paths()
        settings = KronosSettings.from_form(payload) if payload else self.settings()
        self._settings = settings
        if payload:
            self._write_settings(settings)
        report = self.runtime().load(settings)
        if report["loaded"]:
            return _result(True, "Model loaded. It is forecasting only; it cannot trade.")
        return _result(
            False,
            f"Kronos could not be loaded ({report['reason']}). "
            + (f"{report.get('detail')}. " if report.get("detail") else "")
            + "Nothing was downloaded or installed; fix the setting and load again.",
            reason=str(report["reason"]),
        )

    def stop(self) -> dict[str, Any]:
        runtime = self.runtime()
        if runtime is None:
            return _result(True, "Nothing was running.")
        runtime.stop()
        return _result(True, "Stopped. The model is unloaded and scheduled forecasting is off.")

    def forecast(self, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run one forecast on the symbol and timeframe the controls show.

        The input choice belongs to the forecast, not to the load: changing it on a
        loaded model re-points the request and leaves the weights alone.  A change
        that would need different weights is named as one instead of being applied.
        """

        payload = _settings_payload(form)
        if payload:
            merged = {**self.settings().to_payload(), **payload}
            candidate = KronosSettings.from_form(merged)
            problems = candidate.validate(symbols=self._symbols)
            if problems:
                return _result(
                    False,
                    "Nothing was forecast: "
                    + "; ".join(f"{item.path}: {item.message}" for item in problems),
                    reason="settings_invalid",
                )
            self._settings = candidate
            self._write_settings(candidate)
            if self.runtime().view().get("state") in {"READY", "LOADING"}:
                applied = self.runtime().update_settings(candidate)
                if applied.get("reload_required"):
                    return _result(
                        False,
                        "Nothing was forecast: "
                        + ", ".join(applied["reload_required"])
                        + " only take effect after pressing Load model. The symbol and "
                        "timeframe can change without a reload; these cannot.",
                        reason="reload_required",
                    )
        runtime = self.runtime()
        outcome = runtime.forecast_once()
        if not outcome.get("ok"):
            reason = str(outcome.get("reason", ""))
            detail = str(outcome.get("detail", ""))
            return _result(
                False,
                f"No forecast was produced ({reason})"
                + (f": {detail}" if detail else "")
                + ". Nothing was substituted for it.",
                reason=reason,
            )
        forecast = outcome["forecast"]
        return _result(
            True,
            f"Forecast recorded for {forecast.symbol} {forecast.timeframe}: "
            f"median {forecast.expected_move:+.5f} over {forecast.horizon} bar(s), "
            f"band {forecast.band.p10[-1]} to {forecast.band.p90[-1]}. "
            "It is not a signal and created no order.",
        )

    def schedule_start(self, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
        runtime = self.runtime()
        interval = None
        if form:
            settings = KronosSettings.from_form(form)
            interval = int(settings.interval_seconds)
        outcome = runtime.start(interval_seconds=interval)
        if not outcome.get("started"):
            return _result(
                False,
                f"Scheduled forecasting did not start ({outcome.get('reason')}).",
                reason=str(outcome.get("reason", "")),
            )
        return _result(
            True,
            f"Scheduled forecasting is running every {outcome['interval_seconds']}s. "
            "Each result is recorded; none of them can trade.",
        )

    def schedule_stop(self) -> dict[str, Any]:
        runtime = self.runtime()
        runtime.stop_schedule()
        return _result(True, "Scheduled forecasting stopped. The model stays loaded.")

    def preset_save(self, name: str) -> dict[str, Any]:
        try:
            save_preset(self.settings(), name, self.presets_path)
        except PresetError as exc:
            return _result(False, f"Preset not saved ({exc.reason}).", reason=exc.reason)
        return _result(True, f"Preset “{name}” saved locally.")

    def preset_load(self, name: str) -> dict[str, Any]:
        try:
            settings = load_preset(name, self.presets_path)
        except PresetError as exc:
            return _result(False, f"Preset not loaded ({exc.reason}).", reason=exc.reason)
        self._settings = settings
        self._write_settings(settings)
        return _result(
            True,
            f"Preset “{name}” loaded into the form. Press Load model to use it.",
        )

    def reconcile(self) -> dict[str, Any]:
        if self.bars_provider is None:
            return _result(
                False,
                "Outcomes need bars from this feed, and this workstation has no bar "
                "source wired in.",
                reason="no_bar_source",
            )

        def series(symbol: str, timeframe: str):
            bars = list(self.bars_provider(symbol, timeframe, 400))
            return [(bar.timestamp, _mid_close(bar)) for bar in bars]

        report = reconcile(self.store(), series_provider=series, clock=self.clock)
        return _result(
            True,
            f"{report['resolved']} forecast outcome(s) recorded; {report['waiting']} still "
            "waiting for their horizon to elapse.",
        )

    def run_benchmark(
        self,
        *,
        symbol: str = "",
        timeframe: str = "",
        max_steps: int = 40,
        include_kronos: bool = True,
    ) -> dict[str, Any]:
        """Score forecasts against the baselines, per pair and timeframe.

        The window is chronological and out-of-sample, nothing is fitted, and a
        score here is a forecast-accuracy figure -- never a profit claim.
        """

        settings = self.settings()
        symbol = symbol or settings.symbol
        timeframe = timeframe or settings.timeframe
        if self.bars_provider is None:
            return _result(
                False, "A benchmark needs bars from this feed; none are wired in.",
                reason="no_bar_source",
            )
        if not symbol:
            return _result(False, "Choose a symbol first.", reason="symbol_required")
        seconds = TIMEFRAMES.get(timeframe, 0)
        if seconds <= 0:
            return _result(False, f"{timeframe} is not a supported timeframe.", reason="unsupported_timeframe")

        models: list[Any] = list(standard_baselines())
        forecaster = getattr(self.runtime(), "forecaster", None)
        if include_kronos and forecaster is not None:
            models.append(KronosModel(forecaster))
        try:
            bars = list(self.bars_provider(symbol, timeframe, 600))
            result = evaluate(
                bars,
                symbol=symbol,
                timeframe=timeframe,
                bar_seconds=seconds,
                lookback=int(settings.lookback),
                horizon=int(settings.pred_len),
                models=models,
                max_steps=max_steps,
            )
        except ValueError as exc:
            return _result(False, f"Not enough bars to benchmark ({exc}).", reason="insufficient_bars")
        except Exception as exc:
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            return _result(False, f"The benchmark stopped ({reason}).", reason=reason)

        view = {"ran_at": self.clock().isoformat(), **result.audit_view()}
        if include_kronos and forecaster is None:
            view["notes"] = list(view["notes"]) + [
                "Kronos is not loaded, so only the baselines were scored."
            ]
        self._last_benchmark = view
        self._write_benchmark(view)
        return _result(
            True,
            f"Benchmarked {symbol} {timeframe} over {result.steps} step(s) against "
            f"{len(models)} model(s). These are forecast errors, not profits.",
        )

    def _write_benchmark(self, view: Mapping[str, Any]) -> None:
        path = self.benchmark_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"schema": "axiom-kronos-benchmark-v1", "result": dict(view)}
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass

    def act(self, action: str, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch one page action.  Unknown actions are refused, not guessed."""

        payload: Mapping[str, Any] = form if isinstance(form, Mapping) else {}

        def field(name: str) -> str:
            value = payload.get(name)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            return str(value if value is not None else "").strip()

        if action == "kronos-install-check":
            return self.install_check()
        if action == "kronos-install":
            return self.install_start()
        if action == "kronos-install-cancel":
            return self.install_cancel()
        if action == "kronos-install-cleanup":
            return self.install_cleanup()
        if action == "kronos-save":
            return self.save(payload)
        if action == "kronos-load":
            return self.load(payload)
        if action == "kronos-stop":
            return self.stop()
        if action == "kronos-forecast":
            return self.forecast(payload)
        if action == "kronos-schedule-start":
            return self.schedule_start(payload)
        if action == "kronos-schedule-stop":
            return self.schedule_stop()
        if action == "kronos-preset-save":
            return self.preset_save(field("preset_name"))
        if action == "kronos-preset-load":
            return self.preset_load(field("preset_name"))
        if action == "kronos-reconcile":
            return self.reconcile()
        if action == "kronos-inspect-weights":
            return self.inspect_weights(payload)
        if action == "kronos-benchmark":
            return self.run_benchmark(
                symbol=field("symbol"),
                timeframe=field("timeframe"),
                include_kronos=field("include_kronos") not in {"no", ""},
            )
        return _result(False, f"Unknown Kronos action {action!r}.", reason="unknown_action")

    # -- view -----------------------------------------------------------

    def view(self, symbols: Iterable[str] = ()) -> dict[str, Any]:
        self._symbols = tuple(str(item) for item in symbols if str(item).strip())
        settings = self.settings_for(self._symbols)
        runtime = self.runtime()
        problems = [item.audit_view() for item in settings.validate(symbols=list(symbols))]
        availability = probe_availability()
        history: dict[str, Any] = {"recent": [], "summary": {}}
        try:
            history = {
                "recent": self.store().recent(limit=8),
                "summary": self.store().summary(),
            }
        except Exception:
            history = {"recent": [], "summary": {"note": "The forecast history could not be read."}}
        benchmark = self._last_benchmark or self._read_benchmark()
        return {
            "available": True,
            "label": FORECAST_LABEL,
            "configured": self.settings_path.exists(),
            "settings": settings.to_payload(),
            "fingerprint": settings.fingerprint(),
            "controls": settings.controls(),
            "problems": problems,
            "estimate": estimate(settings).audit_view(),
            "availability": availability.audit_view(),
            "state": runtime.view(),
            "schedule_running": runtime.running,
            "presets": list_presets(self.presets_path),
            "install": self.installer().view(settings.model_key),
            "install_schema": INSTALL_SCHEMA,
            "recommended_variant": RECOMMENDED_VARIANT,
            "managed_root": str(managed_root(self.root)),
            "installed_assets": [item.audit_view() for item in recommended_assets(settings.model_key)],
            "symbol_default": settings.symbol,
            "symbol_choices": list(self._symbols),
            "history": history,
            "mapping": describe_mapping(),
            "unsupported": [{"setting": name, "why": why} for name, why in UNSUPPORTED_SETTINGS],
            "variants": [
                {
                    "key": item.key,
                    "name": item.name,
                    "context_length": item.context_length,
                    "parameters": item.parameters,
                    "model_id": item.model_id,
                    "tokenizer_id": item.tokenizer_id,
                }
                for item in selectable_variants()
            ],
            "source": {
                "repo": SOURCE_REPO,
                "commit_url": SOURCE_COMMIT_URL,
                "code_licence": CODE_LICENCE,
                "code_copyright": CODE_LICENCE_COPYRIGHT,
                "paper_url": PAPER_URL,
                "weights_host": WEIGHTS_HOST,
                "weights_licence": WEIGHTS_LICENCE_STATE,
            },
            "requirements": list(RUNTIME_REQUIREMENTS),
            "has_bar_source": self.bars_provider is not None,
            "symbols": [str(item) for item in symbols],
            "timeframes": list(TIMEFRAMES),
            "last": dict(self._last),
            "weights": dict(self._weights) or self._read_weights(),
            "chart": self._chart_view(settings),
            "benchmark": benchmark,
            "baselines": [
                {"key": model.key, "label": model.label, "note": model.note}
                for model in standard_baselines()
            ],
            "notes": [
                "A forecast is an estimate of the next bars, not a trade signal. "
                "Nothing on this page can create an order, size a position, or change "
                "an execution mode.",
                "Kronos is not validated on your broker's data until the outcome "
                "history and the benchmark below have enough scored observations to "
                "say anything. A benchmark score is never evidence of profit.",
                "The predictor averages the paths it is given, so AXIOM runs one "
                "predict() per rollout to keep the spread and report bands. That "
                "multiplies inference time by the rollout count.",
            ],
        }

    def _chart_view(self, settings: KronosSettings) -> dict[str, Any]:
        """Recent mid closes plus the last forecast's band, for the page's chart.

        Read-only, and only when a bar source exists.  A chart of yesterday's
        prices next to today's forecast would be worse than no chart, so the
        forecast shown is the most recent one recorded on this page.
        """

        if self.bars_provider is None:
            return {}
        try:
            bars = list(self.bars_provider(settings.symbol, settings.timeframe, 64))
        except Exception:
            return {}
        if not bars:
            return {}
        history = [
            {"at": bar.timestamp.isoformat(), "close": str(_mid_close(bar))}
            for bar in bars[-60:]
        ]
        payload = self._last_payload
        forecast = {
            "at": payload.get("created_at", ""),
            "points": [point["at"] for point in payload.get("points") or []],
            "p10": [str(value) for value in (payload.get("band") or {}).get("p10") or []],
            "p50": [str(value) for value in (payload.get("band") or {}).get("p50") or []],
            "p90": [str(value) for value in (payload.get("band") or {}).get("p90") or []],
        }
        return {
            "symbol": settings.symbol,
            "timeframe": settings.timeframe,
            "history": history,
            "forecast": forecast if forecast["points"] else {},
            "price_basis": "MID",
        }

    def _read_weights(self) -> dict[str, Any]:
        path = Path(self.root) / "kronos_weights.local.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_benchmark(self) -> dict[str, Any]:
        path = self.benchmark_path
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        result = payload.get("result") if isinstance(payload, dict) else None
        return dict(result) if isinstance(result, dict) else {}


def _mid_close(bar: Any):
    from decimal import Decimal

    bid, _ = bar.bid.parsed()
    ask, _ = bar.ask.parsed()
    return (bid["close"] + ask["close"]) / Decimal(2)


def default_panel(
    root: str | Path,
    *,
    bars_provider: Callable[[str, str, int], Sequence[Any]] | None = None,
    forecaster_factory: Callable[[KronosSettings], Any] | None = None,
    logger: Callable[[str], None] | None = None,
) -> KronosPanel:
    return KronosPanel(
        root=Path(root),
        bars_provider=bars_provider,
        forecaster_factory=forecaster_factory,
        logger=logger,
    )


__all__ = [
    "BENCHMARK_FILENAME",
    "HISTORY_FILENAME",
    "KronosPanel",
    "SETTINGS_FILENAME",
    "SETTINGS_SCHEMA",
    "default_panel",
]
