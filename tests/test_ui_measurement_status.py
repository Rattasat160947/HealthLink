# -*- coding: utf-8 -*-
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
    monkeypatch.setattr(win, "_start_task", lambda *_args: None)
    yield win


@pytest.mark.parametrize(
    ("measure_method", "card_name", "expected_title", "expected_detail"),
    [
        ("_measure_bp", "bp_status_card", "กำลังวัดความดัน", "กรุณาอยู่นิ่งและไม่ขยับแขน"),
        ("_measure_spo2", "spo2_status_card", "กำลังวัดออกซิเจน", "กรุณาวางนิ้วค้างไว้และอยู่นิ่ง"),
        ("_measure_temperature", "temp_status_card", "กำลังวัดอุณหภูมิ", "กรุณาแนบเซนเซอร์กับผิวค้างไว้"),
    ],
)
def test_measurement_shows_thai_busy_card(
    window, measure_method, card_name, expected_title, expected_detail
):
    getattr(window, measure_method)()
    card = getattr(window, card_name)

    assert card.isHidden() is False
    assert card.title_label.text() == expected_title
    assert card.detail_label.text() == expected_detail


@pytest.mark.parametrize(
    ("kind", "done_method", "result", "card_name"),
    [
        ("bp", "_on_bp_done", BloodPressureReading(120, 80, 70), "bp_status_card"),
        ("spo2", "_on_spo2_done", 98, "spo2_status_card"),
        ("temp", "_on_temperature_done", 36.8, "temp_status_card"),
    ],
)
def test_success_changes_card_to_thai_confirmation(window, kind, done_method, result, card_name):
    window._show_measurement_busy(kind)
    getattr(window, done_method)(result)
    card = getattr(window, card_name)

    assert card.isHidden() is False
    assert card.title_label.text() == "วัดสำเร็จ"
    assert card.detail_label.text() == "บันทึกผลการวัดแล้ว"


@pytest.mark.parametrize(
    ("failed_method", "card_name", "expected_detail"),
    [
        ("_on_bp_failed", "bp_status_card", "กรุณาตรวจสอบเครื่องแล้วลองอีกครั้ง"),
        ("_on_spo2_failed", "spo2_status_card", "กรุณาวางนิ้วให้แนบสนิทแล้วลองอีกครั้ง"),
        ("_on_temperature_failed", "temp_status_card", "กรุณาแนบเซนเซอร์กับผิวแล้วลองอีกครั้ง"),
    ],
)
def test_failure_card_uses_short_thai_guidance(window, failed_method, card_name, expected_detail):
    getattr(window, failed_method)("รายละเอียดจากอุปกรณ์")
    card = getattr(window, card_name)

    assert card.isHidden() is False
    assert card.title_label.text() == "วัดไม่สำเร็จ"
    assert card.detail_label.text() == expected_detail


def test_reset_hides_every_measurement_card(window):
    window._show_measurement_busy("bp")
    window._show_measurement_busy("spo2")
    window._show_measurement_busy("temp")

    window._reset_session()

    assert window.bp_status_card.isHidden()
    assert window.spo2_status_card.isHidden()
    assert window.temp_status_card.isHidden()


def test_cards_fit_their_panels_and_render_at_kiosk_size(window, qtbot):
    window.resize(ui_module.WINDOW_WIDTH, ui_module.WINDOW_HEIGHT)
    window.stack.setCurrentIndex(1)
    window.show()
    qtbot.wait(80)

    for kind in ("bp", "spo2", "temp"):
        window._show_measurement_busy(kind)
    qtbot.wait(80)

    assert window.bp_status_card.geometry().right() <= window.nibp_section.width()
    assert window.spo2_status_card.geometry().right() <= (
        window.spo2_row.width() - window.btn_spo2.width()
    )
    assert window.temp_status_card.geometry().right() <= (
        window.temp_row.width() - window.btn_temp.width()
    )

    image = window.grab()
    assert image.isNull() is False
