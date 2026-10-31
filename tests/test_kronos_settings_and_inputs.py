"""Kronos settings and input mapping: what is offered, and what is refused.

The point of these tests is not that a forecast is right; it is that the page can
only offer settings the official predictor actually has, that a model which cannot
run is never offered at all, and that an input the broker did not provide is
labelled absent rather than invented.

All bars here are synthetic fixtures.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.models import ForexQuoteBar, QuoteSideBar
from axiom_forex.kronos.inputs import (
    ABSENT,
    DERIVED,
    MID,
    TICK_VOLUME,
    InputRefused,
    build_input,
    describe_mapping,
)
from axiom_forex.kronos.registry import (
    CODE_LICENCE,
    SOURCE_COMMIT,
    VARIANTS,
    WEIGHTS_LICENCE_STATE,
    selectable_variants,
    variant,
)
from axiom_forex.kronos.settings import (
    KronosSettings,
    PresetError,
    list_presets,
    load_preset,
    save_preset,
)

UTC = timezone.utc
PIP = Decimal("0.0001")
START = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)


def make_bar(mid: Decimal, timestamp: datetime, *, quote_count: int = 12) -> ForexQuoteBar:
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
        quote_count=quote_count,
        last_quote_at=timestamp + timedelta(seconds=59),
        complete=True,
    )


def bars(
    count: int = 60,
    *,
    start: Decimal = Decimal("1.1000"),
    quote_count: int = 12,
) -> list[ForexQuoteBar]:
    values: list[ForexQuoteBar] = []
    mid = start
    for index in range(count):
        values.append(make_bar(mid, START + timedelta(minutes=index), quote_count=quote_count))
        mid += PIP
    return values


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_the_official_facts_are_the_ones_we_read():
    assert CODE_LICENCE == "MIT"
    assert SOURCE_COMMIT == "67b630e"
    # The weights' licence is not something this project can vouch for.
    assert WEIGHTS_LICENCE_STATE == "UNVERIFIED_BY_AXIOM"


def test_only_variants_with_public_weights_are_selectable():
    keys = {item.key for item in selectable_variants()}
    assert keys == {"kronos-mini", "kronos-small", "kronos-base"}
    large = variant("kronos-large")
    assert large is not None and large.open_weights is False

    settings = KronosSettings(model_key="kronos-large", symbol="EURUSD")
    problems = settings.validate()
    assert any(problem.path == "kronos.model" for problem in problems)
    assert any("no official public weights" in problem.message for problem in problems)


def test_an_unknown_model_key_is_refused_not_coerced():
    settings = KronosSettings(model_key="kronos-turbo", symbol="EURUSD")
    assert any("not a published Kronos variant" in item.message for item in settings.validate())


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_context_above_the_variant_limit_is_a_problem_not_a_silent_truncation():
    settings = KronosSettings(model_key="kronos-small", symbol="EURUSD", max_context=4096)
    message = " ".join(item.message for item in settings.validate())
    assert "512" in message
    assert "truncated" in message


def test_tick_timeframes_are_not_offered():
    settings = KronosSettings(symbol="EURUSD", timeframe="1s")
    assert any(item.path == "kronos.timeframe" for item in settings.validate())


def test_a_symbol_outside_the_declared_provider_symbols_is_refused():
    settings = KronosSettings(symbol="GBPJPY")
    problems = settings.validate(symbols=["EURUSD"])
    assert any(item.path == "kronos.symbol" for item in problems)


def test_core_controls_are_grouped_and_carry_the_model_limit():
    settings = KronosSettings(symbol="EURUSD", model_key="kronos-mini")
    controls = settings.controls()
    groups = {item["group"] for item in controls}
    assert groups == {"Basic", "Advanced"}
    by_name = {item["name"]: item for item in controls}
    # The mini variant's real context length, from the registry, not a guess.
    assert by_name["lookback"]["max"] == str(VARIANTS["kronos-mini"].context_length)
    assert "TICK_VOLUME" in by_name["volume_mode"]["options"]
    for name in ("temperature", "top_k", "top_p", "rollouts", "pred_len", "device"):
        assert name in by_name


def test_no_control_exists_for_a_setting_kronos_does_not_support():
    # The predictor has no seed argument, no dtype argument and no beam search.
    names = {item["name"] for item in KronosSettings(symbol="EURUSD").controls()}
    for invented in ("seed_argument", "dtype", "beam_width", "num_beams", "tokenizer"):
        assert invented not in names
    # AXIOM-side controls are marked as such in their help text.
    by_name = {item["name"]: item for item in KronosSettings(symbol="EURUSD").controls()}
    assert "not a model control" in by_name["seed"]["help"] or "AXIOM-side" in by_name["seed"]["help"]


def test_the_fingerprint_changes_with_what_changes_the_numbers():
    base = KronosSettings(symbol="EURUSD")
    assert base.fingerprint() == KronosSettings(symbol="EURUSD").fingerprint()
    assert base.fingerprint() != KronosSettings(symbol="EURUSD", rollouts=6).fingerprint()
    assert base.fingerprint() != KronosSettings(symbol="EURUSD", temperature=Decimal("0.7")).fingerprint()
    # The scheduling interval does not change a forecast's numbers.
    assert base.fingerprint() == KronosSettings(symbol="EURUSD", interval_seconds=60).fingerprint()


def test_a_form_round_trip_keeps_the_decimal_settings_exact():
    form = {
        "model_key": ["kronos-base"],
        "symbol": ["EURUSD"],
        "timeframe": ["5m"],
        "temperature": ["0.75"],
        "top_p": ["1"],
        "rollouts": ["7"],
        "seed": [""],
    }
    settings = KronosSettings.from_form(form)
    assert settings.model_key == "kronos-base"
    assert settings.temperature == Decimal("0.75")
    assert settings.rollouts == 7
    assert settings.seed is None


def test_presets_round_trip_without_touching_the_repository(tmp_path):
    path = tmp_path / "kronos_presets.local.json"
    settings = KronosSettings(symbol="EURUSD", timeframe="15m", rollouts=9)
    save_preset(settings, "eurgrid", path)
    assert list_presets(path) == ["eurgrid"]
    loaded = load_preset("eurgrid", path)
    assert loaded.rollouts == 9
    assert loaded.timeframe == "15m"
    assert loaded.preset == "eurgrid"
    assert loaded.fingerprint() == settings.fingerprint()


def test_a_missing_preset_is_an_error_not_a_default(tmp_path):
    path = tmp_path / "kronos_presets.local.json"
    with pytest.raises(PresetError) as caught:
        load_preset("nope", path)
    assert caught.value.reason == "preset_not_found"


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


def test_the_mapping_is_bid_ask_mid_and_tick_volume_labelled():
    data = build_input(
        bars(40),
        symbol="EURUSD",
        timeframe="1m",
        lookback=40,
        horizon=3,
        bar_seconds=60,
        now=START + timedelta(minutes=39),
    )
    assert data.quality.price_basis == MID
    assert data.quality.volume_mode == TICK_VOLUME
    assert data.quality.amount_mode == DERIVED
    assert data.columns == ("open", "high", "low", "close", "volume", "amount")
    # The mid of a bar whose bid/ask straddle 1.1000 is exactly 1.1000, and the
    # freshest bar is the 40th: 39 pips up from the first.
    assert data.last_close == Decimal("1.1039")
    # Volume is the tick count, not a turnover figure.
    assert data.rows[-1]["volume"] == 12.0


def test_without_a_tick_count_volume_is_absent_not_zero_filled_here():
    data = build_input(
        bars(40, quote_count=0),
        symbol="EURUSD",
        timeframe="1m",
        lookback=40,
        horizon=2,
        bar_seconds=60,
    )
    assert data.quality.volume_mode == ABSENT
    assert data.quality.amount_mode == ABSENT
    assert "volume" not in data.columns
    assert "amount" not in data.columns
    assert any("No tick count" in warning for warning in data.quality.warnings)


def test_the_future_timestamps_are_nominal_and_say_so():
    data = build_input(
        bars(40),
        symbol="EURUSD",
        timeframe="1m",
        lookback=40,
        horizon=4,
        bar_seconds=60,
    )
    assert data.quality.nominal_future_timestamps is True
    assert data.y_timestamps[0] == data.x_timestamps[-1] + timedelta(minutes=1)
    assert len(data.y_timestamps) == 4


def test_gaps_are_counted_and_never_interpolated():
    series = bars(40)
    del series[20]
    data = build_input(
        series,
        symbol="EURUSD",
        timeframe="1m",
        lookback=39,
        horizon=2,
        bar_seconds=60,
    )
    assert data.quality.gaps == 1
    assert data.quality.label() == "DEGRADED"
    assert len(data.rows) == 39


def test_an_incomplete_bar_is_refused_rather_than_used():
    series = bars(40)
    series[-1] = ForexQuoteBar(
        pair="EURUSD",
        timeframe="1m",
        timestamp=series[-1].timestamp,
        duration_seconds=60,
        bid=series[-1].bid,
        ask=series[-1].ask,
        source="KRONOS_TEST_BARS",
        quote_count=1,
        last_quote_at=series[-1].last_quote_at,
        complete=False,
    )
    with pytest.raises(InputRefused) as caught:
        build_input(series, symbol="EURUSD", timeframe="1m", lookback=40, horizon=2, bar_seconds=60)
    assert caught.value.reason == "incomplete_bar"


def test_out_of_order_bars_are_refused():
    series = bars(40)
    series[10], series[11] = series[11], series[10]
    with pytest.raises(InputRefused) as caught:
        build_input(series, symbol="EURUSD", timeframe="1m", lookback=40, horizon=2, bar_seconds=60)
    assert caught.value.reason == "bars_out_of_order"


def test_crossed_quotes_are_refused():
    series = bars(40)
    last = series[-1]
    series[-1] = ForexQuoteBar(
        pair="EURUSD",
        timeframe="1m",
        timestamp=last.timestamp,
        duration_seconds=60,
        bid=QuoteSideBar(open=Decimal("1.2000"), high=Decimal("1.2001"), low=Decimal("1.1999"), close=Decimal("1.2000")),
        ask=QuoteSideBar(open=Decimal("1.1000"), high=Decimal("1.1001"), low=Decimal("1.0999"), close=Decimal("1.1000")),
        source="KRONOS_TEST_BARS",
        quote_count=4,
        last_quote_at=last.last_quote_at,
        complete=True,
    )
    with pytest.raises(InputRefused) as caught:
        build_input(series, symbol="EURUSD", timeframe="1m", lookback=40, horizon=2, bar_seconds=60)
    assert caught.value.reason == "crossed_quotes"


def test_the_page_can_describe_the_mapping_without_running_anything():
    rows = {item["column"]: item for item in describe_mapping()}
    assert rows["close"]["label"] == MID
    assert rows["volume"]["label"] == TICK_VOLUME
    assert rows["amount"]["label"] == DERIVED
    assert "tick count" in rows["volume"]["source"].lower()
    # Every OHLC column states the same, non-tradable basis.
    for column in ("open", "high", "low", "close"):
        assert rows[column]["label"] == MID
