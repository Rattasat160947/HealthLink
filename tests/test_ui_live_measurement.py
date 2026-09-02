# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

import carekeeper_ui as ui_module
from carekeeper_providers import BloodPressureReading, MockCareKeeperProvider
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
    monkeypatch.setattr(win, "_start_task", lambda *_args: None)
    yield win


@pytest.mark.parametrize(
    ("measure_method", "kind", "labels"),
    [
        ("_measure_bp", "bp", ("lbl_sys_value", "lbl_dia_value", "lbl_pulse_value")),
        ("_measure_spo2", "spo2", ("lbl_spo2_value",)),
        ("_measure_temperature", "temp", ("lbl_temp_value",)),
    ],
)
def test_measurement_starts_with_expected_feedback(window, measure_method, kind, labels):
    getattr(window, measure_method)()

    assert window.measurement_active[kind] is True
    assert all(getattr(window, name).text() == "--" for name in labels)
    if kind == "spo2":
        assert window.spo2_measurement_popup.isHidden() is False
        assert window.spo2_measurement_popup.timer.isActive() is True
    elif kind == "temp":
        assert window.temp_measurement_popup.isHidden() is False
        assert window.temp_measurement_popup.timer.isActive() is True


def test_spo2_popup_stays_up_for_real_samples_then_final_value_is_kept(window, qtbot):
    window._measure_spo2()

    window._on_measurement_progress(
        "spo2", 94, {"bpm": 70, "stable": False, "finger_detected": True}
    )
    assert window.lbl_spo2_value.text() == "--"
    assert window.spo2_measurement_popup.isHidden() is False
    assert window.vitals.spo2 is None

    window._on_measurement_progress(
        "spo2", 98, {"bpm": 72, "stable": False, "finger_detected": True}
    )
    assert window.lbl_spo2_value.text() == "--"
    assert "รอจนกว่าจะนิ่ง" in window.lbl_system_message.text()

    window._on_spo2_done(98)
    assert window.measurement_active["spo2"] is False
    assert window.lbl_spo2_value.text() == "98"
    assert window.vitals.spo2 == 98
    qtbot.wait(300)
    assert window.spo2_measurement_popup.isHidden() is True


def test_spo2_without_finger_shows_thai_placement_instruction(window):
    window._measure_spo2()
    window._on_measurement_progress(
        "spo2", None, {"bpm": 0, "stable": False, "finger_detected": False}
    )

    assert window.lbl_spo2_value.text() == "--"
    assert window.spo2_measurement_popup.message == "วางนิ้วให้แนบเซนเซอร์"
    assert "กรุณาวางนิ้วให้แนบเต็มเซนเซอร์" in window.lbl_system_message.text()


def test_spo2_live_hint_names_the_signal_problem(window):
    """The monitor classifies why a window was uncomputable; while the finger
    is still on the sensor is the only moment that advice can change the
    outcome, so it goes on screen instead of "reading the signal"."""
    window._measure_spo2()
    window._on_measurement_progress(
        "spo2",
        None,
        {"bpm": 0, "stable": False, "finger_detected": True, "quality": "SATURATED"},
    )

    assert window.spo2_measurement_popup.message == "กดเบาลง"
    assert "กดนิ้วแรงเกินไป" in window.lbl_system_message.text()


def test_spo2_live_hint_falls_back_when_the_window_is_not_classified(window):
    """A window that simply did not compute keeps the neutral wording -- no
    guessing at a cause the samples do not support."""
    window._measure_spo2()
    window._on_measurement_progress(
        "spo2", None, {"bpm": 0, "stable": False, "finger_detected": True}
    )

    assert window.spo2_measurement_popup.message == "กำลังอ่านสัญญาณ"


def test_temperature_popup_stays_up_for_samples_then_final_value_is_kept(window, qtbot):
    window._measure_temperature()

    window._on_measurement_progress(
        "temp", 35.8, {"stable": False, "in_contact": True}
    )
    assert window.lbl_temp_value.text() == "--"
    assert window.temp_measurement_popup.isHidden() is False

    window._on_measurement_progress(
        "temp", 36.2, {"stable": False, "in_contact": True}
    )
    assert window.lbl_temp_value.text() == "--"

    window._on_temperature_done(36.2)
    assert window.measurement_active["temp"] is False
    assert window.lbl_temp_value.text() == "36.2"
    assert window.vitals.temperature == 36.2
    qtbot.wait(300)
    assert window.temp_measurement_popup.isHidden() is True


def test_temperature_waiting_updates_keep_popup_visible_without_blinking(window, qtbot):
    window._measure_temperature()
    qtbot.wait(130)
    assert window.lbl_temp_value.text() == "--"
    assert window.temp_measurement_popup.isHidden() is False

    for _ in range(5):
        window._on_measurement_progress(
            "temp", 22.0, {"stable": False, "in_contact": False}
        )
        qtbot.wait(40)
        assert window.lbl_temp_value.text() == "--"
        assert window.temp_measurement_popup.isHidden() is False

    assert window.temp_measurement_popup.message == "แนบเซนเซอร์กับผิว"


def test_remeasurement_clears_previous_value_and_failure_does_not_restore_it(window):
    window.vitals.spo2 = 97
    window._refresh_values()
    window._measure_spo2()
    assert window.vitals.spo2 is None
    assert window.lbl_spo2_value.text() == "--"
    assert window.sum_spo2_value.text() == "--"

    window._on_measurement_progress(
        "spo2", 92, {"stable": False, "finger_detected": True}
    )

    window._on_spo2_failed("อ่านค่าไม่สำเร็จ")

    assert window.measurement_active["spo2"] is False
    assert window.lbl_spo2_value.text() == "--"
    assert window.vitals.spo2 is None
    assert window.spo2_measurement_popup.isHidden() is True


def test_pressure_animation_finishes_on_actual_device_result(window):
    window._measure_bp()
    assert window.lbl_sys_value.text() == "--"

    window._on_bp_done(BloodPressureReading(120, 80, 70))

    assert window.measurement_active["bp"] is False
    assert window.lbl_sys_value.text() == "120"
    assert window.lbl_dia_value.text() == "80"
    assert window.lbl_pulse_value.text() == "70"


def test_pressure_numbers_run_for_feedback_and_replace_old_result(window, qtbot):
    window.vitals.systolic = 130
    window.vitals.diastolic = 90
    window.vitals.pulse = 82
    window._refresh_values()

    window._measure_bp()
    assert window.vitals.systolic is None
    assert window.vitals.diastolic is None
    assert window.vitals.pulse is None
    assert window.lbl_sys_value.text() == "--"
    assert window.sum_bp_value.text() == "--/--"

    qtbot.wait(230)
    assert window.lbl_sys_value.text() == "--"

    window._on_measurement_progress("bp", None, {"started": True})
    qtbot.wait(230)
    assert window.lbl_sys_value.text() == "1"
    assert window.lbl_dia_value.text() == "--"
    assert window.lbl_pulse_value.text() == "--"

    qtbot.wait(230)
    assert window.lbl_sys_value.text() == "2"

    window._on_bp_done(BloodPressureReading(118, 76, 69))
    qtbot.wait(1000)
    assert window.lbl_sys_value.text() == "118"
    assert window.lbl_dia_value.text() == "76"
    assert window.lbl_pulse_value.text() == "69"


def test_pressure_sys_sweeps_1_to_160_then_60_and_back_up(window):
    window._measure_bp()
    window._on_measurement_progress("bp", None, {"started": True})
    display = window.bp_sys_live_display
    display.timer.stop()

    upward = []
    for _ in range(160):
        display._tick()
        upward.append(int(display.current))
    assert upward == list(range(1, 161))

    downward = []
    for _ in range(100):
        display._tick()
        downward.append(int(display.current))
    assert downward == list(range(159, 59, -1))

    display._tick()
    assert display.current == 61


def test_reset_stops_live_numbers(window):
    window._measure_spo2()
    window._measure_temperature()
    window._on_measurement_progress(
        "spo2", 95, {"stable": False, "finger_detected": True}
    )
    window._on_measurement_progress(
        "temp", 36.1, {"stable": False, "in_contact": True}
    )

    window._reset_session()

    assert not any(window.measurement_active.values())
    assert window.bp_sys_live_display.timer.isActive() is False
    assert window.spo2_live_display.timer.isActive() is False
    assert window.temp_live_display.timer.isActive() is False
    assert window.spo2_measurement_popup.isHidden() is True
    assert window.temp_measurement_popup.isHidden() is True


def test_mock_provider_progress_crosses_worker_thread_to_visible_label(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        ui_module, "SubmissionQueue", lambda: SubmissionQueue(db_path=tmp_path / "queue.db")
    )
    win = ui_module.CareKeeperWindow(MockCareKeeperProvider(), mode_name="Test")
    qtbot.addWidget(win)
    win.queue_worker.stop()
    win.queue_worker.join(timeout=2)

    win._measure_spo2()
    assert win.measurement_active["spo2"] is True
    assert win.spo2_measurement_popup.isHidden() is False
    assert win.lbl_spo2_value.text() == "--"

    qtbot.waitUntil(lambda: win.measurement_active["spo2"] is False, timeout=1800)
    qtbot.waitUntil(
        lambda: win.lbl_spo2_value.text() == str(win.vitals.spo2), timeout=1800
    )
    assert win.lbl_spo2_value.text() == str(win.vitals.spo2)
    assert win.lbl_system_message.text().startswith("สำเร็จ:")
