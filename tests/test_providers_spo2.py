# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import carekeeper_providers as cp
from carekeeper_retry import SubsystemRegistry


@pytest.fixture
def provider():
    p = cp.RealCareKeeperProvider.__new__(cp.RealCareKeeperProvider)
    p.device_mac = "aa:bb:cc:dd:ee:ff"
    p.on_retry_attempt = None
    p.on_retry_giveup = None
    return p


class _FakeMonitor:
    """Stand-in for lib.spo2_max30102.SpO2Monitor.

    measure_spo2() is the settled reading (None = never stabilized); the
    settling itself is covered by tests/test_spo2_stability.py."""

    def __init__(self, spo2=97, last_error=None, last_ir_dc=0, overflows=0):
        self._spo2 = spo2
        self.m = SimpleNamespace(shutdown=lambda: None)
        self.calls = 0
        # Set by the real monitor when it gives up, so the provider can say
        # which of the three ways it failed.
        self.last_error = last_error
        self.last_ir_dc = last_ir_dc
        self.overflows = overflows
        self.finger_ir_threshold = 10000

    def measure_spo2(self, on_progress=None):
        self.calls += 1
        return self._spo2

    def GetSpO2Sensor(self):
        return (72, self._spo2 or 0, [], [])


class _FailingOpen:
    """Fake _open_spo2_sensor: raise `fail_times` times, then return `monitor`."""

    def __init__(self, fail_times, monitor):
        self.fail_times = fail_times
        self.monitor = monitor
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("i2c open fail")
        return self.monitor


def test_spo2_open_retries_and_succeeds(provider, monkeypatch):
    opener = _FailingOpen(fail_times=2, monitor=_FakeMonitor(spo2=97))
    monkeypatch.setattr(provider, "_open_spo2_sensor", opener)

    attempts = []
    provider.on_retry_attempt = lambda s, a, m: attempts.append((s, a, m))

    result = provider.measure_spo2()

    assert result == 97
    assert opener.calls == 3
    assert attempts == [("spo2", 2, 3), ("spo2", 3, 3)]
    assert SubsystemRegistry.get("spo2").disabled is False


def test_spo2_open_exhausts_and_disables(provider, monkeypatch):
    opener = _FailingOpen(fail_times=99, monitor=_FakeMonitor())
    monkeypatch.setattr(provider, "_open_spo2_sensor", opener)

    with pytest.raises(RuntimeError):
        provider.measure_spo2()

    assert opener.calls == 3
    assert SubsystemRegistry.get("spo2").disabled is True


def test_spo2_read_timeout_raises_without_disabling(provider, monkeypatch):
    """Sensor opens fine but the reading never settles (measure_spo2 -> None).
    The read raises, but the open path is not retried and the subsystem stays
    enabled -- a finger-not-placed timeout is not a hardware fault."""
    opener = _FailingOpen(fail_times=0, monitor=_FakeMonitor(spo2=None))
    monkeypatch.setattr(provider, "_open_spo2_sensor", opener)

    with pytest.raises(RuntimeError):
        provider.measure_spo2()

    assert opener.calls == 1
    assert SubsystemRegistry.get("spo2").disabled is False


def test_spo2_returns_the_settled_value(provider, monkeypatch):
    """The provider hands back whatever the monitor settled on -- it no longer
    does its own polling, so the GUI gets a final value, not a first sample."""
    monitor = _FakeMonitor(spo2=98)
    monkeypatch.setattr(provider, "_open_spo2_sensor", lambda: monitor)

    assert provider.measure_spo2() == 98
    assert monitor.calls == 1
    assert SubsystemRegistry.get("spo2").disabled is False


def test_spo2_open_passes_read_timeout_to_monitor(provider, monkeypatch):
    """_SPO2_READ_TIMEOUT is the single knob for how long a measurement may
    take; it has to reach the monitor, which owns the settling loop now."""
    created = {}

    class _Monitor:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.m = SimpleNamespace(shutdown=lambda: None)

        def measure_spo2(self, on_progress=None):
            return 96

    fake_pkg = types.ModuleType("lib.spo2_max30102")
    fake_pkg.SpO2Monitor = _Monitor
    monkeypatch.setitem(sys.modules, "lib.spo2_max30102", fake_pkg)
    provider._SPO2_READ_TIMEOUT = 12.0

    assert provider.measure_spo2() == 96
    assert created == {"max_wait_seconds": 12.0}


# ---- why a SpO2 read failed ----


def test_no_finger_failure_names_the_placement_and_shows_the_ir_level(provider, monkeypatch):
    """A finger that reads as absent is how a mis-tuned FINGER_IR_THRESHOLD
    presents, so the level and the cutoff both go in the message -- that pair
    is what CAREKEEPER_SPO2_FINGER_IR_THRESHOLD gets set against."""
    monitor = _FakeMonitor(spo2=None, last_error="NO_FINGER", last_ir_dc=7400)
    monkeypatch.setattr(provider, "_open_spo2_sensor", lambda: monitor)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_spo2()

    message = str(excinfo.value)
    assert "ไม่พบนิ้ว" in message
    assert "IR=7400/10000" in message


def test_unstable_failure_asks_the_operator_to_hold_still(provider, monkeypatch):
    """The opposite instruction from the no-finger case: contact was fine."""
    monitor = _FakeMonitor(spo2=None, last_error="UNSTABLE")
    monkeypatch.setattr(provider, "_open_spo2_sensor", lambda: monitor)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_spo2()

    message = str(excinfo.value)
    assert "ยังไม่นิ่ง" in message
    assert "IR=" not in message  # the IR level explains nothing here


def test_weak_signal_failure_is_its_own_message(provider, monkeypatch):
    monitor = _FakeMonitor(spo2=None, last_error="WEAK_SIGNAL")
    monkeypatch.setattr(provider, "_open_spo2_sensor", lambda: monitor)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_spo2()

    assert "อ่อนเกินไป" in str(excinfo.value)


def test_discarded_fifo_gaps_are_reported(provider, monkeypatch):
    """Dropped samples mean the Pi could not keep up with the sensor, which is
    a different problem from anything the operator can fix by holding still."""
    monitor = _FakeMonitor(spo2=None, last_error="UNSTABLE", overflows=3)
    monkeypatch.setattr(provider, "_open_spo2_sensor", lambda: monitor)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_spo2()

    assert "3 ครั้ง" in str(excinfo.value)


def test_unknown_failure_reason_falls_back_to_the_generic_message(provider, monkeypatch):
    monitor = _FakeMonitor(spo2=None, last_error=None)
    monkeypatch.setattr(provider, "_open_spo2_sensor", lambda: monitor)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_spo2()

    assert "อ่านค่า SpO2 ไม่สำเร็จ" in str(excinfo.value)


# ---- temperature (DS18B20) ----


class _FakeTempSensor:
    def __init__(self, *a, **k):
        pass

    def measure_body_temperature(self, on_progress=None):
        return 36.7


def test_measure_temperature_returns_value(provider, monkeypatch):
    monkeypatch.setattr("lib.temp_sensor.temp_sensor", _FakeTempSensor)
    assert provider.measure_temperature() == 36.7


def test_measure_temperature_none_raises(provider, monkeypatch):
    class _NoStable(_FakeTempSensor):
        def measure_body_temperature(self, on_progress=None):
            return None

    monkeypatch.setattr("lib.temp_sensor.temp_sensor", _NoStable)
    with pytest.raises(RuntimeError):
        provider.measure_temperature()


def test_measure_temperature_sensor_not_found_raises(provider, monkeypatch):
    def _raise(*a, **k):
        raise Exception("temp sensor is not found")

    monkeypatch.setattr("lib.temp_sensor.temp_sensor", _raise)
    with pytest.raises(RuntimeError):
        provider.measure_temperature()
