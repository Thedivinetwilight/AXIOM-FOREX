import json
from dataclasses import replace
from datetime import timedelta

from app.audit.log import sanitize_audit_value
from app.calendar.trading import MarketStatus
from fixtures.m1 import EVALUATED_AT, PAIR, local_utc, make_calendar


def test_calendar_is_valid_and_open_during_core_session():
    calendar = make_calendar()
    assert calendar.validate().valid
    state = calendar.status(PAIR, EVALUATED_AT)
    assert state.status == MarketStatus.OPEN
    assert state.is_open
    assert state.reason == "session_open"


def test_calendar_signature_is_stable_and_versioned():
    calendar = make_calendar()
    assert calendar.signature() == calendar.signature()
    assert replace(calendar, version="calendar-v2").signature() != calendar.signature()
    assert json.dumps(sanitize_audit_value(calendar.status(PAIR, EVALUATED_AT).audit_view()))


def test_calendar_daily_break_and_reopen():
    calendar = make_calendar()
    during_break = calendar.status(PAIR, local_utc(2026, 3, 6, 17, 30))
    assert during_break.status == MarketStatus.DAILY_BREAK
    after_break = calendar.status(PAIR, local_utc(2026, 3, 6, 18, 30))
    assert after_break.status == MarketStatus.OPEN


def test_calendar_weekend_gap_and_sunday_reopen():
    calendar = make_calendar()
    assert calendar.status(PAIR, local_utc(2026, 3, 7, 12, 0)).status == MarketStatus.WEEKEND_GAP
    assert calendar.status(PAIR, local_utc(2026, 3, 8, 16, 59)).status == MarketStatus.WEEKEND_GAP
    assert calendar.status(PAIR, local_utc(2026, 3, 8, 17, 30)).status == MarketStatus.OPEN


def test_calendar_holiday_overrides_session_windows():
    calendar = make_calendar()
    assert calendar.status(PAIR, local_utc(2026, 3, 9, 10, 0)).status == MarketStatus.HOLIDAY
    assert calendar.status(PAIR, EVALUATED_AT).status == MarketStatus.OPEN


def test_calendar_not_yet_valid_and_expired():
    calendar = make_calendar()
    before = calendar.status(PAIR, calendar.valid_from - timedelta(seconds=1))
    assert before.status == MarketStatus.NOT_YET_VALID
    expired = calendar.status(PAIR, calendar.valid_until)
    assert expired.status == MarketStatus.CALENDAR_EXPIRED


def test_calendar_unavailable_instrument_fails_closed():
    calendar = make_calendar()
    state = calendar.status("XXX/YYY", EVALUATED_AT)
    assert state.status == MarketStatus.INSTRUMENT_UNAVAILABLE
    assert not state.is_open


def test_calendar_windows_are_dst_aware_utc_instants():
    calendar = make_calendar()
    pre_dst = local_utc(2026, 3, 6, 17, 0)
    post_dst = local_utc(2026, 3, 12, 17, 0)
    assert pre_dst.hour == 22  # UTC-5 before the DST transition
    assert post_dst.hour == 21  # UTC-4 after the DST transition
    windows = calendar.windows_by_pair[PAIR]
    assert any(window.end == pre_dst for window in windows)
    assert any(window.end == post_dst for window in windows)


def test_calendar_rejects_overlapping_windows():
    calendar = make_calendar()
    first = calendar.windows_by_pair[PAIR][0]
    broken = replace(calendar, windows_by_pair={PAIR: (first, first)})
    result = broken.validate()
    assert not result.valid
    assert any("overlapping_windows_for" in error for error in result.errors)


def test_calendar_rejects_empty_validity_and_unknown_timezone():
    calendar = make_calendar()
    assert not replace(calendar, valid_until=calendar.valid_from).validate().valid
    unknown = replace(calendar, timezone_name="Not/AValidZone")
    assert not unknown.validate().valid
    assert unknown.status(PAIR, EVALUATED_AT).status == MarketStatus.UNKNOWN
