# -*- coding: utf-8 -*-
"""NIBP button cooldown (carekeeper_ui.CareKeeperWindow).

The cuff locks itself out for roughly two minutes after it reports BP_ERROR.
That wait used to be hidden by BPMonitor.measure()'s full 120 s timeout; now
that a BP_ERROR unblocks measure() as soon as it arrives, the GUI has to hold
the button itself or the operator just collects NOT_READY replies.
"""
from __future__ import annotations

import pytest

import carekeeper_ui as ui_module
from carekeeper_providers import BloodPressureReading
from carekeeper_queue import SubmissionQueue
from tests.fakes.fake_provider import FakeFailingProvider


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(
        ui_module, "SubmissionQueue", lambda: SubmissionQueue(db_path=tmp_path / "queue.db")
    )
    win = ui_module.CareKeeperWindow(FakeFailingProvider(), mode_name="Test")
    qtbot.addWidget(win)
    win.queue_worker.stop()
    win.queue_worker.join(timeout=2)
    yield win


def _run_cooldown_to_completion(window):
    """Drive the 1 Hz countdown to zero without spending the wall-clock time."""
    window.bp_cooldown_seconds = 1
    window._bp_cooldown_tick()


# ── success path (unchanged behaviour, pinned) ────────────────────────────

def test_success_starts_the_short_cooldown(window):
    window._on_bp_done(BloodPressureReading(systolic=120, diastolic=80, pulse=70))

    assert window.bp_cooldown_seconds == window.BP_COOLDOWN_AFTER_SUCCESS
    assert window.cooldown_timer.isActive()
    assert window.btn_bp.isEnabled() is False


def test_success_cooldown_ends_on_the_measured_state(window):
    window._on_bp_done(BloodPressureReading(systolic=120, diastolic=80, pulse=70))

    _run_cooldown_to_completion(window)

    assert window.cooldown_timer.isActive() is False
    assert window.btn_bp.objectName() == "BtnNIBPDone"
    assert window.btn_bp.isEnabled() is True


# ── device-error path ─────────────────────────────────────────────────────

def test_device_error_holds_the_button_for_the_cuff_lockout(window):
    window.provider.last_bp_error = "BP_ERROR"

    window._on_bp_failed("เครื่องวัดความดันแจ้งข้อผิดพลาด")

    assert window.bp_cooldown_seconds == window.BP_COOLDOWN_AFTER_DEVICE_ERROR
    assert window.cooldown_timer.isActive()
    assert window.btn_bp.isEnabled() is False


def test_device_error_cooldown_blocks_a_fresh_measurement(window):
    window.provider.last_bp_error = "BP_ERROR"
    window._on_bp_failed("เครื่องวัดความดันแจ้งข้อผิดพลาด")

    window._measure_bp()

    assert window.tasks == []  # the click never reached the provider


def test_device_error_cooldown_ends_on_the_failed_state_not_the_measured_one(window):
    """Regression: the tick used to hardcode "วัดแล้ว" as the end state, which
    after a failure would have claimed a reading that was never taken."""
    window.provider.last_bp_error = "BP_ERROR"
    window._on_bp_failed("เครื่องวัดความดันแจ้งข้อผิดพลาด")

    _run_cooldown_to_completion(window)

    assert window.btn_bp.objectName() == "BtnNIBPFail"
    assert window.btn_bp.isEnabled() is True


# ── everything else stays immediately retryable ───────────────────────────

@pytest.mark.parametrize("reason", ["TIMEOUT", "NOT_READY", None])
def test_non_device_errors_leave_the_button_free(window, reason):
    """Only the cuff's own lockout justifies holding the button. A silent
    bridge or an unplugged cable is worth retrying straight away."""
    window.provider.last_bp_error = reason

    window._on_bp_failed("เครื่องวัดความดันไม่ตอบสนอง")

    assert window.bp_cooldown_seconds == 0
    assert window.cooldown_timer.isActive() is False
    assert window.btn_bp.objectName() == "BtnNIBPFail"
    assert window.btn_bp.isEnabled() is True


def test_session_reset_clears_a_running_cooldown(window):
    window.provider.last_bp_error = "BP_ERROR"
    window._on_bp_failed("เครื่องวัดความดันแจ้งข้อผิดพลาด")

    window._reset_session()

    assert window.bp_cooldown_seconds == 0
    assert window.cooldown_timer.isActive() is False
    assert window.btn_bp.objectName() == "BtnNIBP"
    assert window.btn_bp.isEnabled() is True


def test_no_result_holds_the_button_like_a_device_error(window):
    """A run that ended without a reading still ran the full cycle, so the
    module is in the same post-run lockout a BP_ERROR leaves behind."""
    window.provider.last_bp_error = "NO_RESULT"

    window._on_bp_failed("เครื่องวัดความดันวัดครบรอบแล้วแต่ไม่ได้ส่งค่ากลับมา")

    assert window.bp_cooldown_seconds == window.BP_COOLDOWN_AFTER_DEVICE_ERROR
    assert window.cooldown_timer.isActive()
    assert window.btn_bp.isEnabled() is False


def test_success_cooldown_clears_the_modules_power_down_window(window):
    """The module needs ~60 s after the reading before it accepts another run,
    so the cooldown has to sit above that, not exactly on it."""
    window._on_bp_done(BloodPressureReading(systolic=120, diastolic=80, pulse=70))

    assert window.bp_cooldown_seconds > 60
