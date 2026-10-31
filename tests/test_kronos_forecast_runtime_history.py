"""Forecast, runtime, and history behaviour.

A fake predictor stands in for the model everywhere here, so these tests are about
*this project's* contract with Kronos: the arguments it forwards, the refusal paths
when the runtime is absent, the fact that nothing retries or downloads by itself,
and the requirement that a stored forecast can be scored later without being
rewritten.

No model weights exist in this environment, and nothing here claims a forecast is
accurate.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.models import ForexQuoteBar, QuoteSideBar
from axiom_forex.kronos.forecast import (
    FORECAST_LABEL,
    Forecast,
    KronosInferenceError,
    KronosUnavailable,
    LocalKronosForecaster,
)
from axiom_forex.kronos.history import ForecastStore, Outcome, reconcile, score_forecast
from axiom_forex.kronos.inputs import build_input
from axiom_forex.kronos.runtime import (
    FAILED,
    READY,
    STOPPED,
    KronosRuntime,
    estimate,
    probe_availability,
)
from axiom_forex.kronos.settings import KronosSettings

UTC = timezone.utc
PIP = Decimal("0.0001")
START = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def make_bar(mid: Decimal, timestamp: datetime) -> ForexQuoteBar:
    half = PIP / Decimal(2)
    bid = mid - half
    ask = mid + half
    return ForexQuoteBar(
        pair="EURUSD",
        timeframe="1m",
        timestamp=timestamp,
        duration_seconds=60,
        bid=QuoteSideBar(open=bid, high=bid + half, low=bid - half, close=bid),
        ask=QuoteSideBar(open=ask, high=ask + half, low=ask - half, close=ask),
        source="KRONOS_TEST_BARS",
        quote_count=9,
        last_quote_at=timestamp + timedelta(seconds=59),
        complete=True,
    )


def make_input(*, count: int = 64, horizon: int = 4, timeframe: str = "1m", symbol: str = "EURUSD"):
    series = []
    mid = Decimal("1.1000")
    for index in range(count):
        series.append(make_bar(mid, START + timedelta(minutes=index)))
        mid += PIP / Decimal(4)
    return build_input(
        series,
        symbol=symbol,
        timeframe=timeframe,
        lookback=count,
        horizon=horizon,
        bar_seconds=60,
        now=START + timedelta(minutes=count - 1),
    )


class FakePredictor:
    """Returns ``pred_len`` rows, offset by ``step`` per rollout and bar."""

    def __init__(self, *, step: float = 0.0001, rows: int | None = None, bad: str = "") -> None:
        self.step = step
        self.rows = rows
        self.bad = bad
        self.calls: list[dict[str, object]] = []
        self.device = "cpu"

    def predict(self, **kwargs):
        self.calls.append(kwargs)
        horizon = self.rows if self.rows is not None else int(kwargs["pred_len"])
        base = 1.1000 + self.step * len(self.calls)
        output = []
        for index in range(horizon):
            close = base + self.step * index
            if self.bad == "nan":
                close = float("nan")
            row = {
                "open": close - 0.00001,
                "high": close + 0.00002,
                "low": close - 0.00002,
                "close": close,
            }
            if self.bad == "inverted":
                row["high"] = close - 0.0001
                row["low"] = close + 0.0001
            output.append(row)
        return output


def fake_frames(data):
    return [tuple(row.values()) for row in data.rows], data.x_timestamps, data.y_timestamps


def build_forecaster(predictor, settings=None, **kwargs):
    settings = settings or KronosSettings(
        symbol="EURUSD", timeframe="1m", lookback=64, pred_len=4, rollouts=5
    )
    return LocalKronosForecaster(
        settings,
        predictor_factory=lambda _settings: predictor,
        frame_builder=fake_frames,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# forecast
# ---------------------------------------------------------------------------


def test_one_predict_call_per_rollout_so_the_spread_is_kept():
    predictor = FakePredictor(step=0.00005)
    forecaster = build_forecaster(predictor)
    forecaster.load()
    result = forecaster.forecast(make_input())

    assert len(predictor.calls) == 5
    for call in predictor.calls:
        # The library's own defaults would average several paths into one; asking
        # for a single path per call is what makes a band possible.
        assert call["sample_count"] == 1
        assert call["verbose"] is False
        assert call["pred_len"] == 4
    # Every rollout moved the path, so the band is not degenerate.
    assert result.band.p10[-1] < result.band.p90[-1]
    assert result.label == FORECAST_LABEL
    assert result.rollouts == 5


def test_the_predictor_settings_are_forwarded_unchanged():
    predictor = FakePredictor()
    settings = KronosSettings(
        symbol="EURUSD",
        timeframe="5m",
        lookback=64,
        pred_len=3,
        rollouts=2,
        temperature=Decimal("0.7"),
        top_k=4,
        top_p=Decimal("0.85"),
    )
    forecaster = build_forecaster(predictor, settings)
    forecaster.load()
    forecaster.forecast(make_input())
    call = predictor.calls[0]
    assert call["T"] == 0.7
    assert call["top_k"] == 4
    assert call["top_p"] == 0.85


def test_a_forecast_carries_its_provenance_and_its_quality_report():
    forecaster = build_forecaster(FakePredictor())
    forecaster.load()
    result = forecaster.forecast(make_input())

    assert result.symbol == "EURUSD"
    assert result.model_key == "kronos-mini"
    assert result.model_revision == "local"
    assert result.settings_fingerprint == forecaster.settings.fingerprint()
    assert result.quality["volume_mode"] == "TICK_VOLUME"
    assert result.quality["price_basis"] == "MID"
    payload = result.to_payload()
    assert payload["label"] == FORECAST_LABEL
    assert Forecast.from_payload(payload).audit_view()["forecast_id"] == result.forecast_id


def test_a_short_or_non_finite_output_is_an_error_not_a_short_forecast():
    short = FakePredictor(rows=2)
    forecaster = build_forecaster(short)
    forecaster.load()
    with pytest.raises(KronosInferenceError) as caught:
        forecaster.forecast(make_input())
    assert caught.value.reason == "short_output"

    nan = FakePredictor(bad="nan")
    forecaster = build_forecaster(nan)
    forecaster.load()
    with pytest.raises(KronosInferenceError) as caught:
        forecaster.forecast(make_input())
    assert caught.value.reason == "non_finite_output"

    inverted = FakePredictor(bad="inverted")
    forecaster = build_forecaster(inverted)
    forecaster.load()
    with pytest.raises(KronosInferenceError) as caught:
        forecaster.forecast(make_input())
    assert caught.value.reason == "inverted_output_range"


def test_a_forecast_without_a_loaded_model_is_refused():
    forecaster = build_forecaster(FakePredictor())
    with pytest.raises(KronosUnavailable) as caught:
        forecaster.forecast(make_input())
    assert caught.value.reason == "model_not_loaded"


def test_loading_refuses_settings_that_do_not_validate():
    predictor = FakePredictor()
    forecaster = LocalKronosForecaster(
        KronosSettings(model_key="kronos-large", symbol=""),
        predictor_factory=lambda _settings: predictor,
        frame_builder=fake_frames,
    )
    with pytest.raises(KronosUnavailable) as caught:
        forecaster.load()
    assert caught.value.reason == "settings_invalid"
    assert predictor.calls == []


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------


def test_the_availability_probe_never_imports_a_dependency():
    report = probe_availability()
    names = {item.import_name for item in report.packages}
    assert {"torch", "pandas", "numpy", "einops", "safetensors"} <= names
    # This sandbox has no torch: the probe must say so, and must not have tried.
    if not report.torch_present:
        assert "torch>=2.0.0" in report.missing
        assert report.can_run is False


def test_the_cost_estimate_is_a_range_with_its_assumptions_attached():
    view = estimate(KronosSettings(symbol="EURUSD")).audit_view()
    assert view["parameters"] > 0
    assert view["weights_megabytes"] > 0
    assert any("fp32" in note for note in view["notes"])
    assert any("Activation memory is not estimated" in note for note in view["notes"])


class FakeForecaster:
    def __init__(self, settings):
        self.settings = settings
        self.loaded = False
        self.unloaded = False
        self.forecasts = 0

    def load(self):
        self.loaded = True
        return {"device": "cpu", "seconds": 0.0}

    def unload(self):
        self.unloaded = True

    def forecast(self, data):
        self.forecasts += 1
        base = Decimal("1.1000")
        band = type(
            "Band",
            (),
            {
                "p10": (base,),
                "p50": (base + Decimal("0.0002"),),
                "p90": (base + Decimal("0.0004"),),
                "at": lambda self, index: {},
            },
        )()
        return Forecast(
            forecast_id=f"f{self.forecasts}",
            created_at=START.isoformat(),
            symbol=data.symbol,
            timeframe=data.timeframe,
            model_key=self.settings.model_key,
            model_reference=self.settings.model_reference,
            model_revision="local",
            settings_fingerprint=self.settings.fingerprint(),
            device="cpu",
            lookback=len(data.rows),
            horizon=1,
            rollouts=1,
            points=(),
            band=band,
            last_close=data.last_close,
            last_observed_at=data.quality.last_at,
            quality=data.quality.audit_view(),
            runtime_seconds=0.01,
        )


def make_runtime(**kwargs):
    seen = {"built": 0, "sunk": []}

    def factory(settings):
        seen["built"] += 1
        return FakeForecaster(settings)

    def sink(forecast):
        seen["sunk"].append(forecast.forecast_id)

    runtime = KronosRuntime(
        forecaster_factory=factory,
        inputs_provider=lambda _settings: make_input(),
        on_forecast=sink,
        **kwargs,
    )
    return runtime, seen


def test_a_failed_load_is_sticky_and_is_never_retried_automatically():
    runtime, seen = make_runtime()
    result = runtime.load(KronosSettings(symbol="", model_key="kronos-small"))
    assert result["loaded"] is False
    assert result["reason"] == "settings_invalid"
    assert seen["built"] == 0
    assert runtime.view()["state"] == FAILED
    # Asking again does not silently try a second time behind the operator's back.
    again = runtime.load(KronosSettings(symbol=""))
    assert again["loaded"] is False


def test_load_ready_forecast_and_stop_is_a_complete_cycle():
    runtime, seen = make_runtime()
    result = runtime.load(KronosSettings(symbol="EURUSD", lookback=64, pred_len=4))
    assert result["loaded"] is True
    assert runtime.view()["state"] == READY

    outcome = runtime.forecast_once()
    assert outcome["ok"] is True
    assert seen["sunk"] == ["f1"]
    assert runtime.view()["forecasts"] == 1

    stopped = runtime.stop()
    assert stopped["state"] == STOPPED
    assert runtime.view()["state"] == STOPPED
    assert runtime.forecast_once()["reason"] == "model_not_loaded"


def test_a_factory_that_raises_produces_a_curated_failure_not_a_traceback():
    def explode(_settings):
        raise RuntimeError("boom: /home/user/secret/path")

    runtime = KronosRuntime(
        forecaster_factory=explode,
        inputs_provider=lambda _settings: make_input(),
    )
    result = runtime.load(KronosSettings(symbol="EURUSD"))
    assert result["loaded"] is False
    assert result["reason"] == "RuntimeError"
    assert "secret" not in str(result)


def test_scheduling_refuses_until_the_model_is_ready():
    runtime, _ = make_runtime()
    assert runtime.start()["reason"] == "model_not_loaded"


def test_scheduling_runs_immediately_and_stops_on_request():
    runtime, seen = make_runtime()
    runtime.load(KronosSettings(symbol="EURUSD", lookback=64, pred_len=4, interval_seconds=60))
    started = runtime.start(interval_seconds=60)
    assert started["started"] is True
    deadline = time.time() + 5
    while not seen["sunk"] and time.time() < deadline:
        time.sleep(0.02)
    assert seen["sunk"], "the first forecast should not wait a whole interval"
    assert runtime.running is True
    runtime.stop_schedule()
    assert runtime.running is False
    assert runtime.view()["state"] == READY


def test_stop_also_halts_a_running_schedule():
    runtime, _ = make_runtime()
    runtime.load(KronosSettings(symbol="EURUSD", lookback=64, pred_len=4))
    runtime.start(interval_seconds=60)
    assert runtime.running is True
    runtime.stop()
    assert runtime.running is False


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def make_forecast(forecast_id: str = "k1", *, last_close="1.1000", p50="1.1020") -> Forecast:
    band = type(
        "Band",
        (),
        {
            "p10": (Decimal("1.0990"),),
            "p50": (Decimal(p50),),
            "p90": (Decimal("1.1050"),),
            "at": lambda self, index: {},
        },
    )()
    return Forecast(
        forecast_id=forecast_id,
        created_at=datetime(2026, 3, 10, 9, 0, tzinfo=UTC).isoformat(),
        symbol="EURUSD",
        timeframe="1m",
        model_key="kronos-small",
        model_reference="NeoQuasar/Kronos-small",
        model_revision="local",
        settings_fingerprint="abc123",
        device="cpu",
        lookback=64,
        horizon=1,
        rollouts=5,
        points=(),
        band=band,
        last_close=Decimal(last_close),
        last_observed_at=datetime(2026, 3, 10, 8, 59, tzinfo=UTC).isoformat(),
        quality={"price_basis": "MID"},
        runtime_seconds=0.5,
    )


def test_a_stored_forecast_is_unchanged_by_later_settings(tmp_path):
    store = ForecastStore(tmp_path / "kronos.sqlite3")
    forecast = make_forecast()
    store.record(forecast)
    stored = store.recent()[0]
    assert stored["settings_fingerprint"] == "abc123"
    assert stored["model_reference"] == "NeoQuasar/Kronos-small"
    # Recording twice is idempotent rather than duplicating history.
    store.record(forecast)
    assert len(store.recent()) == 1


def test_no_outcome_yet_is_reported_as_no_outcome(tmp_path):
    store = ForecastStore(tmp_path / "kronos.sqlite3")
    store.record(make_forecast())
    summary = store.summary()
    assert summary["scored"] == 0
    assert "nothing to score" in summary["note"]


def test_scoring_waits_for_the_horizon_instead_of_guessing():
    payload = make_forecast().to_payload()
    payload["points"] = [{"at": datetime(2026, 3, 10, 9, 5, tzinfo=UTC).isoformat()}]
    partial = [(datetime(2026, 3, 10, 9, 2, tzinfo=UTC), Decimal("1.1010"))]
    assert score_forecast(payload, partial) is None


def test_a_scored_forecast_reports_direction_and_error_but_no_profit():
    payload = make_forecast().to_payload()
    payload["points"] = [{"at": datetime(2026, 3, 10, 9, 1, tzinfo=UTC).isoformat()}]
    series = [
        (datetime(2026, 3, 10, 9, 0, tzinfo=UTC), Decimal("1.1000")),
        (datetime(2026, 3, 10, 9, 1, tzinfo=UTC), Decimal("1.1010")),
    ]
    outcome = score_forecast(payload, series)
    assert isinstance(outcome, Outcome)
    assert outcome.direction_correct is True
    assert outcome.absolute_error == Decimal("0.0010")
    assert outcome.band_covered is True
    assert "not a profit figure" in outcome.note
    assert "profit" not in outcome.audit_view() or "not a profit figure" in outcome.audit_view()["note"]


def test_reconcile_scores_only_what_has_data_and_reports_what_is_waiting(tmp_path):
    store = ForecastStore(tmp_path / "kronos.sqlite3")
    done = make_forecast("done")
    waiting = make_forecast("waiting")

    def with_points(forecast, at):
        payload = forecast.to_payload()
        payload["points"] = [{"at": at.isoformat()}]
        return payload

    done_payload = with_points(done, datetime(2026, 3, 10, 9, 1, tzinfo=UTC))
    waiting_payload = with_points(waiting, datetime(2026, 3, 10, 12, 0, tzinfo=UTC))
    from axiom_forex.kronos.forecast import Forecast as F

    store.record(F.from_payload(done_payload))
    store.record(F.from_payload(waiting_payload))

    series = [
        (datetime(2026, 3, 10, 9, 1, tzinfo=UTC), Decimal("1.1010")),
        (datetime(2026, 3, 10, 9, 2, tzinfo=UTC), Decimal("1.1015")),
    ]
    report = reconcile(store, series_provider=lambda _symbol, _timeframe: series)
    assert report == {"resolved": 1, "waiting": 1}
    summary = store.summary()
    assert summary["scored"] == 1
    assert summary["direction_rate"] == 1.0
    assert "no claim about future accuracy" in summary["note"]

    # Scoring twice must not double-count.
    assert reconcile(store, series_provider=lambda _s, _t: series)["resolved"] == 0
    assert store.summary()["scored"] == 1


# ---------------------------------------------------------------------------
# the live leg's data-only surface
# ---------------------------------------------------------------------------


class _Spec:
    canonical_pair = "EURUSD"
    pair = "EURUSD"


class _Adapter:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def bars(self, spec, timeframe, *, start, end):
        self.calls.append((spec, timeframe, start, end))
        return list(self.rows)


def _leg(adapter, **kwargs):
    from axiom_forex.paper_runner.live import LiveLeg

    return LiveLeg(
        router=None,
        adapter=adapter,
        gate=None,
        mode=None,
        spec=_Spec(),
        strategy_id="test",
        **kwargs,
    )


def test_the_live_leg_serves_bars_without_handing_out_the_adapter():
    adapter = _Adapter([1, 2, 3])
    leg = _leg(adapter)
    rows = leg.bars("EURUSD", "1m", count=5)
    assert rows == [1, 2, 3]
    # The window is derived from the leg's own clock, and only bars were read.
    assert len(adapter.calls) == 1
    assert adapter.calls[0][1] == "1m"


def test_the_live_leg_refuses_a_symbol_it_is_not_configured_for():
    from axiom_forex.paper_runner.live import LiveDataRefused

    leg = _leg(_Adapter([]))
    with pytest.raises(LiveDataRefused) as caught:
        leg.bars("GBPJPY", "1m", count=5)
    assert caught.value.reason == "symbol_not_on_terminal"


def test_the_live_leg_reports_no_terminal_rather_than_inventing_bars():
    from axiom_forex.paper_runner.live import LiveDataRefused

    leg = _leg(None)
    with pytest.raises(LiveDataRefused) as caught:
        leg.bars("EURUSD", "1m", count=5)
    assert caught.value.reason == "no_terminal"


def test_an_empty_read_is_a_refusal_not_an_empty_forecast():
    from axiom_forex.paper_runner.live import LiveDataRefused

    leg = _leg(_Adapter([]))
    with pytest.raises(LiveDataRefused) as caught:
        leg.bars("EURUSD", "1m", count=5)
    assert caught.value.reason == "no_bars"
