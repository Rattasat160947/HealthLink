# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

import pytest

import carekeeper_ui as ui_module
from carekeeper_queue import SubmissionQueue
from tests.fakes.fake_provider import FakeFailingProvider


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(ui_module, "SubmissionQueue", lambda: SubmissionQueue(db_path=tmp_path / "queue.db"))
    provider = FakeFailingProvider()
    win = ui_module.CareKeeperWindow(provider, mode_name="Test")
    qtbot.addWidget(win)
    # Stop the background drain worker so it doesn't race with the
    # assertions below — each test exercises _submit_data's own immediate
    # send path deterministically.
    win.queue_worker.stop()
    win.queue_worker.join(timeout=2)
    yield win


def _set_full_vitals(window) -> None:
    window.vitals.systolic = 120
    window.vitals.diastolic = 80
    window.vitals.pulse = 70
    window.vitals.spo2 = 98


def test_submit_data_enqueues_before_send_attempt(window):
    _set_full_vitals(window)
    window._submit_data()
    # Enqueue is synchronous (local disk write) and happens before the
    # background ProviderTask even starts, so this is true immediately.
    assert window.submission_queue.count_pending() == 1


def test_submit_data_success_path_clears_queue_row(window, qtbot):
    _set_full_vitals(window)
    window._submit_data()
    qtbot.waitUntil(lambda: window.submission_queue.count_pending() == 0, timeout=3000)
    assert window.provider.sent_payloads[0]["mac"] == window.provider.device_mac


def test_submit_data_failure_path_leaves_row_for_background_worker(window, qtbot):
    window.provider.send_data_exception = RuntimeError("network down")
    _set_full_vitals(window)
    window._submit_data()
    qtbot.waitUntil(lambda: window.btn_finish.isEnabled(), timeout=3000)
    # Data stays queued for the background worker — the operator is not
    # blocked waiting for the network per the confirmed UX decision.
    assert window.submission_queue.count_pending() == 1


# ── measurement time in the payload ───────────────────────────────────────

def _sent_payload(window, qtbot) -> dict:
    qtbot.waitUntil(lambda: bool(window.provider.sent_payloads), timeout=3000)
    return window.provider.sent_payloads[0]


def test_submit_payload_carries_the_time_of_measurement(window, qtbot):
    _set_full_vitals(window)
    window.vitals.bp_measured_at = datetime(2026, 9, 1, 20, 15, 33)
    window.vitals.spo2_measured_at = datetime(2026, 9, 1, 20, 15, 33)

    window._submit_data()

    assert _sent_payload(window, qtbot)["measured_at"] == "2026-09-01 20:15:33"


def test_measured_at_is_the_last_probe_to_finish(window, qtbot):
    """The three probes finish minutes apart; the set is only complete at the
    last of them, and that is the moment the record describes."""
    _set_full_vitals(window)
    window.vitals.bp_measured_at = datetime(2026, 9, 1, 20, 10, 0)
    window.vitals.spo2_measured_at = datetime(2026, 9, 1, 20, 13, 30)
    window.vitals.temp_measured_at = datetime(2026, 9, 1, 20, 11, 45)

    window._submit_data()

    assert _sent_payload(window, qtbot)["measured_at"] == "2026-09-01 20:13:30"


def test_measured_at_falls_back_to_now_when_nothing_was_stamped(window, qtbot):
    """A payload always carries a time, even if it somehow reaches submit with
    no stamped measurement -- the backend must never have to guess."""
    before = datetime.now().replace(microsecond=0)
    _set_full_vitals(window)

    window._submit_data()

    stamped = datetime.strptime(
        _sent_payload(window, qtbot)["measured_at"], window.MEASURED_AT_FORMAT
    )
    assert stamped >= before


def test_queued_payload_keeps_the_measurement_time_not_the_delivery_time(window):
    """The whole point of stamping client-side: an offline payload can sit in
    the queue for hours, and it must still say when the reading was taken."""
    window.provider.send_data_exception = RuntimeError("network down")
    _set_full_vitals(window)
    window.vitals.bp_measured_at = datetime(2026, 9, 1, 20, 15, 33)

    window._submit_data()

    queued = window.submission_queue.peek_pending()[0].payload
    assert queued["measured_at"] == "2026-09-01 20:15:33"


def test_measurements_are_stamped_when_they_land(window):
    before = datetime.now()

    window._on_spo2_done(98)
    window._on_temperature_done(36.5)

    assert window.vitals.spo2_measured_at >= before
    assert window.vitals.temp_measured_at >= before


def test_clearing_a_measurement_clears_its_stamp(window):
    """A re-measure must not leave the previous run's time attached to a value
    that no longer exists."""
    window._on_spo2_done(98)

    window._clear_measurement_value("spo2")

    assert window.vitals.spo2 is None
    assert window.vitals.spo2_measured_at is None


# ── temperature ───────────────────────────────────────────────────────────

def test_submit_payload_carries_the_temperature(window, qtbot):
    """Regression: the kiosk measured a temperature, required it before the
    submit button unlocked, and then dropped it on the floor."""
    _set_full_vitals(window)
    window.vitals.temperature = 36.5

    window._submit_data()

    assert _sent_payload(window, qtbot)["temperature"] == 36.5


def test_submit_payload_rounds_the_temperature_to_one_decimal(window, qtbot):
    """The sensor returns full float precision; one decimal is what the screen
    shows and what a body temperature means."""
    _set_full_vitals(window)
    window.vitals.temperature = 36.4499999

    window._submit_data()

    assert _sent_payload(window, qtbot)["temperature"] == 36.4


def test_submit_payload_sends_null_temperature_rather_than_omitting_it(window, qtbot):
    """Same shape every time: a missing reading is an explicit null, like the
    other vitals, not an absent key the backend has to special-case."""
    _set_full_vitals(window)  # leaves temperature unset

    window._submit_data()

    payload = _sent_payload(window, qtbot)
    assert "temperature" in payload
    assert payload["temperature"] is None
