# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import types

from carekeeper_providers import RealCareKeeperProvider


def _provider() -> RealCareKeeperProvider:
    return RealCareKeeperProvider.__new__(RealCareKeeperProvider)


def test_battery_state_reports_charging(monkeypatch):
    class FakeUPS:
        STATUS_CHARGING = "CHARGING"
        STATUS_FAST_CHARGING = "FAST_CHARGING"
        STATUS_VBUS_POWERED = "VBUS_POWERED"

        def get_battery_percent(self):
            return 84

        def get_status(self):
            return self.STATUS_CHARGING

    monkeypatch.setitem(sys.modules, "lib.ups", types.SimpleNamespace(UPSHat=FakeUPS))

    assert _provider()._read_battery_state() == (84, True)


def test_battery_state_reports_not_charging(monkeypatch):
    class FakeUPS:
        STATUS_CHARGING = "CHARGING"
        STATUS_FAST_CHARGING = "FAST_CHARGING"
        STATUS_VBUS_POWERED = "VBUS_POWERED"

        def get_battery_percent(self):
            return 61

        def get_status(self):
            return "DISCHARGING"

    monkeypatch.setitem(sys.modules, "lib.ups", types.SimpleNamespace(UPSHat=FakeUPS))

    assert _provider()._read_battery_state() == (61, False)


def test_battery_state_reports_charging_when_vbus_power_is_present(monkeypatch):
    class FakeUPS:
        STATUS_CHARGING = "CHARGING"
        STATUS_FAST_CHARGING = "FAST_CHARGING"
        STATUS_VBUS_POWERED = "VBUS_POWERED"

        def get_battery_percent(self):
            return 100

        def get_status(self):
            return self.STATUS_VBUS_POWERED

    monkeypatch.setitem(sys.modules, "lib.ups", types.SimpleNamespace(UPSHat=FakeUPS))

    assert _provider()._read_battery_state() == (100, True)


def test_battery_state_falls_back_and_logs_the_hardware_error_once(monkeypatch, capsys):
    class BrokenUPS:
        STATUS_CHARGING = "CHARGING"
        STATUS_FAST_CHARGING = "FAST_CHARGING"

        def __init__(self):
            raise OSError("I2C unavailable")

    monkeypatch.setitem(sys.modules, "lib.ups", types.SimpleNamespace(UPSHat=BrokenUPS))

    provider = _provider()
    assert provider._read_battery_state() == (None, False)
    assert provider._read_battery_state() == (None, False)

    output = capsys.readouterr().out
    assert output.count("[Battery] read failed") == 1
    assert "OSError: I2C unavailable" in output
