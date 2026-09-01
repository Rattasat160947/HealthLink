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
def test_measurement_starts_inside_value_area_without_popup(window, measure_method, kind, labels):
    getattr(window, measure_method)()

    assert window.measurement_active[kind] is True
    assert all(getattr(window, name).text() == "--" for name in labels)
    assert not hasattr(window, "spo2_status_card")


def test_spo2_real_samples_move_the_visible_number_until_final(window, qtbot):
    window._measure_spo2()

    window._on_measurement_progress(
        "spo2", 94, {"bpm": 70, "stable": False, "finger_detected": True}
    )
    assert window.lbl_spo2_value.text() == "94"
    assert window.vitals.spo2 is None

    window._on_measurement_progress(
        "spo2", 98, {"bpm": 72, "stable": False, "finger_detected": True}
    )
    qtbot.wait(350)
    assert window.lbl_spo2_value.text() == "98"
    assert "รอจนกว่าจะนิ่ง" in window.lbl_system_message.text()

    window._on_spo2_done(98)
    assert window.measurement_active["spo2"] is False
    assert window.lbl_spo2_value.text() == "98"
    assert window.vitals.spo2 == 98


def test_spo2_without_finger_shows_thai_placement_instruction(window):
    window._measure_spo2()
    window._on_measurement_progress(
        "spo2", None, {"bpm": 0, "stable": False, "finger_detected": False}
    )

    assert window.lbl_spo2_value.text() == "--"
    assert "กรุณาวางนิ้วให้แนบเต็มเซนเซอร์" in window.lbl_system_message.text()


def test_temperature_real_samples_move_smoothly_then_settle(window, qtbot):
    window._measure_temperature()

    window._on_measurement_progress(
        "temp", 35.8, {"stable": False, "in_contact": True}
    )
    assert window.lbl_temp_value.text() == "35.8"

    window._on_measurement_progress(
        "temp", 36.2, {"stable": False, "in_contact": True}
    )
    qtbot.wait(350)
    assert window.lbl_temp_value.text() == "36.2"

    window._on_temperature_done(36.2)
    assert window.measurement_active["temp"] is False
    assert window.lbl_temp_value.text() == "36.2"
    assert window.vitals.temperature == 36.2


def test_failed_live_measurement_restores_the_previous_final_value(window):
    window.vitals.spo2 = 97
    window._refresh_values()
    window._measure_spo2()
    window._on_measurement_progress(
        "spo2", 92, {"stable": False, "finger_detected": True}
    )

    window._on_spo2_failed("อ่านค่าไม่สำเร็จ")

    assert window.measurement_active["spo2"] is False
    assert window.lbl_spo2_value.text() == "97"


def test_pressure_uses_only_the_final_value_because_device_has_no_live_samples(window):
    window._measure_bp()
    assert window.lbl_sys_value.text() == "--"

    window._on_bp_done(BloodPressureReading(120, 80, 70))

    assert window.measurement_active["bp"] is False
    assert window.lbl_sys_value.text() == "120"
    assert window.lbl_dia_value.text() == "80"
    assert window.lbl_pulse_value.text() == "70"


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
    assert window.spo2_live_display.timer.isActive() is False
    assert window.temp_live_display.timer.isActive() is False


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
    qtbot.waitUntil(lambda: win.lbl_spo2_value.text() != "--", timeout=1800)
    assert win.measurement_active["spo2"] is True

    qtbot.waitUntil(lambda: win.measurement_active["spo2"] is False, timeout=1800)
    assert win.lbl_spo2_value.text() == str(win.vitals.spo2)
    assert win.lbl_system_message.text().startswith("สำเร็จ:")
