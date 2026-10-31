"""The 24-hour qualification clock: what counts, what pauses, what fails.

Every test drives a fake clock, so a day takes microseconds and the arithmetic is
exact.  No terminal, adapter, or order object appears anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from axiom_forex.paper_runner.qualification import (
    FAILED,
    FAILURE_CYCLE,
    NOT_STARTED,
    PASSED,
    PAUSE_DISCONNECTED,
    PAUSE_RUNNER_RESTARTED,
    PAUSE_STALE,
    PAUSE_UNRESPONSIVE,
    RUNNING,
    STOPPED,
    QualificationClock,
    QualificationPolicy,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def run_healthy(clock: Clock, qualification: QualificationClock, *, seconds: float, step: float = 60.0) -> None:
    remaining = seconds
    while remaining > 0:
        gap = min(step, remaining)
        clock.advance(gap)
        qualification.tick(healthy=True, quote_fresh=True, decisions=1)
        remaining -= gap


def _fresh(tmp_path, **kwargs) -> tuple[QualificationClock, Clock]:
    clock = Clock()
    return QualificationClock(tmp_path, clock=clock, **kwargs), clock


# -- the happy path ---------------------------------------------------------


def test_nothing_is_available_before_a_test_is_started(tmp_path):
    qualification, _clock = _fresh(tmp_path)
    assert qualification.verdict() == NOT_STARTED
    assert qualification.live_available() is False
    assert qualification.blockers()
    assert "no qualification test" in qualification.summary_line().lower()


def test_a_full_day_of_healthy_time_passes(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start(symbols=["EURUSD.pro"], candidates=["cat-a"])
    run_healthy(clock, qualification, seconds=24 * 3600)
    report = qualification.report()
    assert report["active_seconds"] == 24 * 3600
    assert report["verdict"] == PASSED
    assert report["live_available"] is True
    assert report["criteria_met"] == report["criteria_total"]
    assert {item["key"] for item in report["criteria"]} >= {
        "active_time",
        "wall_span",
        "pause_share",
        "stale_share",
        "decisions",
        "failures",
        "restarts",
    }
    assert "not proof of profitability" in report["claim"]


def test_a_day_of_paused_time_does_not_pass(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    for _ in range(24 * 60):
        clock.advance(60)
        qualification.tick(healthy=False, reason=PAUSE_DISCONNECTED, quote_fresh=False)
    report = qualification.report()
    assert report["active_seconds"] == 0
    assert report["paused_seconds"] > 0
    assert report["verdict"] == RUNNING
    assert report["live_available"] is False


def test_a_stale_quote_pauses_the_clock_and_is_counted(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    run_healthy(clock, qualification, seconds=3600)
    clock.advance(60)
    reason = qualification.tick(healthy=False, reason=PAUSE_STALE, quote_fresh=False)
    assert reason == PAUSE_STALE
    clock.advance(60)
    qualification.tick(healthy=True, quote_fresh=True)
    report = qualification.report()
    assert report["pause_seconds_by_reason"][PAUSE_STALE] >= 60
    assert report["stale_ticks"] == 1
    assert report["healthy_ticks"] >= 60
    # The hour counted; the stale minute did not.
    assert 3600 <= report["active_seconds"] < 3720


def test_a_long_gap_is_never_counted_as_activity_however_healthy_the_next_tick(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    run_healthy(clock, qualification, seconds=600)
    clock.advance(6 * 3600)  # laptop asleep
    qualification.tick(healthy=True, quote_fresh=True)
    report = qualification.report()
    assert report["active_seconds"] == 600
    assert report["pause_seconds_by_reason"][PAUSE_UNRESPONSIVE] >= 6 * 3600 - 1
    assert report["verdict"] == RUNNING


# -- failures ---------------------------------------------------------------


def test_a_recorded_criterion_breaker_fails_the_test(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    run_healthy(clock, qualification, seconds=24 * 3600 + 600)
    assert qualification.verdict() == PASSED
    qualification.record_failure(FAILURE_CYCLE, "stage=decide")
    assert qualification.verdict() == FAILED
    assert qualification.live_available() is False
    assert any(item["reason"] == "qualification_failed" for item in qualification.blockers())


def test_too_much_downtime_fails_the_pass_criteria(tmp_path):
    qualification, clock = _fresh(
        tmp_path, policy=QualificationPolicy(required_active_seconds=3600, required_span_seconds=3600)
    )
    qualification.start()
    run_healthy(clock, qualification, seconds=1800)
    # 70% of the span now paused: over the 30% allowance.
    for _ in range(70):
        clock.advance(60)
        qualification.tick(healthy=False, reason=PAUSE_DISCONNECTED, quote_fresh=False)
    report = qualification.report()
    pause = next(item for item in report["criteria"] if item["key"] == "pause_share")
    assert pause["met"] is False
    assert report["verdict"] in (RUNNING, STOPPED)


def test_a_day_with_no_decision_is_not_a_test(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    for _ in range(24 * 60):
        clock.advance(60)
        qualification.tick(healthy=True, quote_fresh=True, decisions=0)
    report = qualification.report()
    assert report["active_seconds"] >= 24 * 3600 - 60
    decisions = next(item for item in report["criteria"] if item["key"] == "decisions")
    assert decisions["met"] is False
    assert report["verdict"] == RUNNING


# -- stopping, restarting, resetting ---------------------------------------


def test_stopping_keeps_every_number_and_the_clock_stops(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    run_healthy(clock, qualification, seconds=3600)
    qualification.stop()
    before = qualification.state()["active_seconds"]
    clock.advance(3600)
    qualification.tick(healthy=True, quote_fresh=True)
    assert qualification.state()["active_seconds"] == before
    assert qualification.verdict() == STOPPED


def test_a_completed_and_stopped_test_still_reads_as_passed(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    run_healthy(clock, qualification, seconds=24 * 3600 + 120)
    qualification.stop()
    assert qualification.verdict() == PASSED
    assert qualification.live_available() is True


def test_a_restart_does_not_count_the_dark_period(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    run_healthy(clock, qualification, seconds=3600)
    clock.advance(5 * 3600)  # the process was gone
    assert qualification.recover()["status"] == RUNNING
    report = qualification.report()
    assert report["active_seconds"] == 3600
    assert report["paused_seconds"] >= 5 * 3600
    assert report["pause_seconds_by_reason"][PAUSE_RUNNER_RESTARTED] >= 5 * 3600
    assert report["restarts"] == 1
    # Counting resumes after the recovery.
    run_healthy(clock, qualification, seconds=600)
    assert qualification.report()["active_seconds"] == 4200


def test_recovery_is_persisted_so_the_record_survives_a_new_process(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start(symbols=["EURUSD.pro"])
    run_healthy(clock, qualification, seconds=1800)
    clock.advance(3600)

    revived = QualificationClock(tmp_path, clock=clock)
    assert revived.state()["status"] == RUNNING
    assert revived.state()["active_seconds"] == 1800
    revived.recover()
    assert revived.report()["pause_seconds_by_reason"][PAUSE_RUNNER_RESTARTED] >= 3600
    assert revived.report()["active_seconds"] == 1800


def test_reset_discards_the_record_and_records_that_it_did(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    run_healthy(clock, qualification, seconds=3600)
    assert qualification.reset()["status"] == NOT_STARTED
    assert qualification.verdict() == NOT_STARTED
    assert qualification.state()["events"][-1]["kind"] == "reset"


def test_the_policy_is_fixed_at_start_and_survives_a_restart(tmp_path):
    policy = QualificationPolicy(required_active_seconds=7200, min_decisions=3)
    qualification, clock = _fresh(tmp_path)
    qualification.start(policy=policy)
    clock.advance(60)
    qualification.tick(healthy=True, quote_fresh=True, decisions=1)
    revived = QualificationClock(tmp_path, clock=clock)
    assert revived.policy.required_active_seconds == 7200
    assert revived.policy.min_decisions == 3
    criteria = {item.key: item for item in revived.criteria()}
    assert criteria["active_time"].required.startswith("at least 2.00 h")


def test_the_record_contains_no_identifiers_or_balances(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start(symbols=["EURUSD.pro"], candidates=["cat-a"])
    run_healthy(clock, qualification, seconds=600)
    text = (tmp_path / "qualification.json").read_text(encoding="utf-8")
    for forbidden in ("password", "login", "balance", "equity", "secret", "token"):
        assert forbidden not in text.lower()


def test_an_unreadable_record_degrades_to_not_started(tmp_path):
    qualification, _clock = _fresh(tmp_path)
    qualification.path.parent.mkdir(parents=True, exist_ok=True)
    qualification.path.write_text("{not json", encoding="utf-8")
    fresh = QualificationClock(tmp_path)
    assert fresh.verdict() == NOT_STARTED
    assert fresh.state()["active_seconds"] == 0


def test_a_policy_stated_for_a_new_test_beats_the_one_in_an_old_record(tmp_path):
    strict = QualificationPolicy(required_active_seconds=86400, min_decisions=1)
    loose = QualificationPolicy(required_active_seconds=0, required_span_seconds=0, min_decisions=0)

    first, clock = _fresh(tmp_path, policy=strict)
    first.start()
    assert first.verdict() == RUNNING
    first.stop()

    second = QualificationClock(tmp_path, policy=loose, clock=clock)
    second.start()
    assert second.policy.required_active_seconds == 0
    assert second.verdict() == PASSED


def test_pausing_an_unhealthy_tick_with_no_reason_is_still_a_pause(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    clock.advance(60)
    reason = qualification.tick(healthy=False, quote_fresh=False)
    assert reason == "unhealthy_unknown"
    report = qualification.report()
    assert report["active_seconds"] == 0
    assert report["pause_seconds_by_reason"]["unhealthy_unknown"] == 60


def test_a_stop_is_a_pause_and_the_counted_time_survives_it(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    run_healthy(clock, qualification, seconds=3600)
    qualification.stop()
    stopped = qualification.state()["active_seconds"]
    clock.advance(2 * 3600)
    assert qualification.resume()["status"] == RUNNING
    report = qualification.report()
    assert report["active_seconds"] == stopped
    assert report["pause_seconds_by_reason"]["runner_stopped"] >= 2 * 3600
    assert report["verdict"] == RUNNING
    run_healthy(clock, qualification, seconds=600)
    assert qualification.report()["active_seconds"] == stopped + 600


def test_a_failed_record_is_never_reopened(tmp_path):
    qualification, clock = _fresh(tmp_path)
    qualification.start()
    run_healthy(clock, qualification, seconds=60)
    qualification.stop(failed=True)
    assert qualification.resume()["status"] == FAILED
    assert qualification.verdict() == FAILED
    assert qualification.live_available() is False
