# -*- coding: utf-8 -*-
from __future__ import annotations

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
    """Stand-in for lib.spo2_max30102.SpO2Monitor."""

    def __init__(self, spo2=97):
        self._spo2 = spo2
        self.m = SimpleNamespace(shutdown=lambda: None)

    def GetSpO2Sensor(self):
        return (72, self._spo2, [], [])


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
    """Sensor opens fine but never yields a valid finger reading (all 0). The
    read loop times out and raises, but the open path is not retried and the
    subsystem stays enabled -- a finger-not-placed timeout is not a fault."""
    opener = _FailingOpen(fail_times=0, monitor=_FakeMonitor(spo2=0))
    monkeypatch.setattr(provider, "_open_spo2_sensor", opener)
    provider._SPO2_READ_TIMEOUT = 0.2  # don't actually wait the full 30s

    with pytest.raises(RuntimeError):
        provider.measure_spo2()

    assert opener.calls == 1
    assert SubsystemRegistry.get("spo2").disabled is False


def test_spo2_skips_out_of_range_then_accepts_valid(provider, monkeypatch):
    """A flagged-but-unphysiological value (40) is skipped; polling continues
    until a value inside the 70-100 window appears."""
    monitor = _FakeMonitor()
    seq = iter([(72, 40, [], []), (72, 40, [], []), (72, 98, [], [])])
    monitor.GetSpO2Sensor = lambda: next(seq)
    monkeypatch.setattr(provider, "_open_spo2_sensor", lambda: monitor)

    assert provider.measure_spo2() == 98
    assert SubsystemRegistry.get("spo2").disabled is False


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
