# -*- coding: utf-8 -*-
"""Unit tests for the Waveshare UPS HAT driver (lib/ups.py).

lib/ups.py imports `smbus` (Raspberry Pi I2C) at module level, so a fake
smbus module is injected into sys.modules before import. Register maps are
scripted per test to verify the byte→value decoding: little-endian 16-bit
fields, the signed-current conversion, and the status bit flags.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


class FakeSMBus:
    """Replacement for smbus.SMBus returning scripted register blocks."""

    registers: dict[int, list[int]] = {}

    def __init__(self, bus_num):
        self.bus_num = bus_num
        self.written: list[tuple[int, int, int]] = []

    def read_i2c_block_data(self, addr, reg, length):
        return list(self.registers[reg][:length])

    def write_byte_data(self, addr, reg, value):
        self.written.append((addr, reg, value))


@pytest.fixture
def ups_module(monkeypatch):
    fake_smbus = types.ModuleType("smbus")
    fake_smbus.SMBus = FakeSMBus
    monkeypatch.setitem(sys.modules, "smbus", fake_smbus)
    sys.modules.pop("lib.ups", None)
    module = importlib.import_module("lib.ups")
    yield module
    sys.modules.pop("lib.ups", None)


@pytest.fixture
def ups(ups_module):
    FakeSMBus.registers = {}
    return ups_module.UPSHat()


# ── Status register 0x02 bit flags ────────────────────────────────────────

def test_status_idle_when_no_flag_bits(ups):
    FakeSMBus.registers[0x02] = [0x00]
    assert ups.get_status() == "IDLE"


def test_status_fast_charging_bit_0x40(ups):
    FakeSMBus.registers[0x02] = [0x40]
    assert ups.get_status() == "FAST_CHARGING"


def test_status_charging_bit_0x80(ups):
    FakeSMBus.registers[0x02] = [0x80]
    assert ups.get_status() == "CHARGING"


def test_status_vbus_powered_bit_0x20(ups):
    FakeSMBus.registers[0x02] = [0x20]
    assert ups.get_status() == "VBUS_POWERED"


def test_status_fast_charging_takes_priority_over_charging(ups):
    FakeSMBus.registers[0x02] = [0xC0]
    assert ups.get_status() == "FAST_CHARGING"


# ── 16-bit little-endian field decoding ───────────────────────────────────

def test_battery_voltage_is_little_endian(ups):
    # bytes [0x10, 0x0F] → 0x0F10 = 3856 mV
    FakeSMBus.registers[0x20] = [0x10, 0x0F] + [0] * 10
    assert ups.get_battery_voltage() == 3856


def test_battery_percent_reads_bytes_4_and_5(ups):
    FakeSMBus.registers[0x20] = [0, 0, 0, 0, 85, 0] + [0] * 6
    assert ups.get_battery_percent() == 85


def test_battery_percent_100(ups):
    FakeSMBus.registers[0x20] = [0, 0, 0, 0, 100, 0] + [0] * 6
    assert ups.get_battery_percent() == 100


def test_vbus_voltage_current_power_fields(ups):
    # 5 V (0x1388), 1 A (0x03E8), 5 W (0x1388) packed little-endian
    FakeSMBus.registers[0x10] = [0x88, 0x13, 0xE8, 0x03, 0x88, 0x13]
    assert ups.get_vbus_voltage() == 5000
    assert ups.get_vbus_current() == 1000
    assert ups.get_vbus_power() == 5000


def test_cell_voltages_map_four_cells(ups):
    FakeSMBus.registers[0x30] = [0x04, 0x10, 0x08, 0x10, 0x0C, 0x10, 0x10, 0x10]
    cells = ups.get_cell_voltages()
    assert cells == {"V1": 0x1004, "V2": 0x1008, "V3": 0x100C, "V4": 0x1010}


# ── Signed current conversion ─────────────────────────────────────────────

def test_battery_current_positive_when_charging(ups):
    FakeSMBus.registers[0x20] = [0, 0, 0xF4, 0x01] + [0] * 8  # +500 mA
    assert ups.get_battery_current() == 500


def test_battery_current_negative_when_discharging(ups):
    # raw 0xFE0C (65036) > 0x7FFF → 65036 - 0xFFFF = -499 mA
    FakeSMBus.registers[0x20] = [0, 0, 0x0C, 0xFE] + [0] * 8
    assert ups.get_battery_current() == -499


# ── get_all branch selection ──────────────────────────────────────────────

def _fill_common_registers(charging: bool):
    FakeSMBus.registers[0x02] = [0x80 if charging else 0x20]
    FakeSMBus.registers[0x10] = [0] * 6
    current = [0xF4, 0x01] if charging else [0x0C, 0xFE]
    FakeSMBus.registers[0x20] = [0, 0] + current + [80, 0, 0, 0, 90, 0, 45, 0]
    FakeSMBus.registers[0x30] = [0] * 8


def test_get_all_reports_time_to_full_while_charging(ups):
    _fill_common_registers(charging=True)
    info = ups.get_all()
    assert info["status"] == "CHARGING"
    assert info["time_to_full"] == 45
    assert "runtime_to_empty" not in info


def test_get_all_reports_runtime_to_empty_while_discharging(ups):
    _fill_common_registers(charging=False)
    info = ups.get_all()
    assert info["status"] == "VBUS_POWERED"
    assert info["runtime_to_empty"] == 90
    assert "time_to_full" not in info


# ── Power management writes ───────────────────────────────────────────────

def test_enable_auto_power_on_writes_0x55_to_register_0x01(ups):
    ups.enable_auto_power_on()
    assert ups.bus.written == [(0x2D, 0x01, 0x55)]
