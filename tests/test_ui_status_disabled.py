# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

import carekeeper_ui as ui_module
from carekeeper_providers import DeviceStatus
from carekeeper_queue import SubmissionQueue
from carekeeper_retry import SubsystemRegistry
from tests.fakes.fake_provider import FakeFailingProvider


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(ui_module, "SubmissionQueue", lambda: SubmissionQueue(db_path=tmp_path / "queue.db"))
    provider = FakeFailingProvider()
    win = ui_module.CareKeeperWindow(provider, mode_name="Test")
    qtbot.addWidget(win)
    win.queue_worker.stop()
    win.queue_worker.join(timeout=2)
    yield win


def test_device_status_disabled_greys_out_wifi_indicator(window):
    status = DeviceStatus(wifi_connected=True, wifi_disabled=True, bluetooth_connected=True)
    window._on_status_done(status)

    wifi_clusters = [w for w in window._status_widgets if w[1] is not None]
    assert wifi_clusters  # the welcome header still carries a Wi-Fi indicator
    for _bt, wifi, _battery, _battery_text, _bt_text, wifi_text in wifi_clusters:
        assert wifi.connected is False
        assert wifi_text.text() == "ปิดใช้งาน"


def test_device_status_disabled_greys_out_bp_button(window):
    status = DeviceStatus(bp_disabled=True)
    window._on_status_done(status)

    assert window.btn_bp.objectName() == "BtnNIBPDisabled"
    # Still clickable so the operator can manually trigger a fresh retry cycle.
    assert window.btn_bp.isEnabled() is True


def test_device_status_clears_disabled_styling_when_reenabled(window):
    window._on_status_done(DeviceStatus(bp_disabled=True))
    assert window.btn_bp.objectName() == "BtnNIBPDisabled"

    window._on_status_done(DeviceStatus(bp_disabled=False))
    assert window.btn_bp.objectName() == "BtnNIBP"


def test_clicking_disabled_wifi_indicator_reenables_subsystem(window, qtbot):
    SubsystemRegistry.get("wifi").disable("simulated failure")
    assert SubsystemRegistry.get("wifi").disabled is True

    window._open_wifi_selector()

    assert SubsystemRegistry.get("wifi").disabled is False
    qtbot.wait(100)


def test_clicking_disabled_bp_button_reenables_subsystem(window, qtbot):
    SubsystemRegistry.get("bp_monitor").disable("simulated failure")
    assert SubsystemRegistry.get("bp_monitor").disabled is True

    window._measure_bp()

    assert SubsystemRegistry.get("bp_monitor").disabled is False
    qtbot.wait(100)


def test_status_poll_does_not_block_on_disabled_subsystem(window):
    """Regression guard: feeding a disabled status into _on_status_done is a
    cheap widget update, not a retry trigger — it must not call back into
    retry_with_notify or otherwise stall."""
    for _ in range(20):
        window._on_status_done(DeviceStatus(wifi_disabled=True, bluetooth_disabled=True, bp_disabled=True, spo2_disabled=True, idcard_disabled=True))
    assert SubsystemRegistry.get("wifi").consecutive_failures == 0
