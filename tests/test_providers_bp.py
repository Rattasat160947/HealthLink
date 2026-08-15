# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

import serial
import pytest

import carekeeper_providers as cp
from carekeeper_retry import SubsystemRegistry
from tests.fakes.fake_serial import FakeSerialFactory


def _fake_port(device, vid=0x1A86):
    """Stand-in for serial.tools.list_ports.comports() entries."""
    return SimpleNamespace(device=device, vid=vid, pid=None, description=device)


@pytest.fixture
def provider(monkeypatch):
    p = cp.RealCareKeeperProvider.__new__(cp.RealCareKeeperProvider)
    p.device_mac = "aa:bb:cc:dd:ee:ff"
    p.bp_port = "/dev/fake-port"
    p.on_retry_attempt = None
    p.on_retry_giveup = None
    # measure_blood_pressure now auto-detects the port; keep the configured fake
    # port "present" so the connect-retry tests below exercise only that path.
    monkeypatch.setattr(
        "serial.tools.list_ports.comports", lambda: [_fake_port("/dev/fake-port")]
    )
    return p


def test_bp_connect_retries_on_serial_exception_then_succeeds(provider, monkeypatch):
    factory = FakeSerialFactory(fail_times=2, lines=["SYS:120,DIA:80,PUL:70", "READY"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    result = provider.measure_blood_pressure()

    assert factory.calls == 3
    assert result.systolic == 120
    assert result.diastolic == 80
    assert result.pulse == 70
    assert SubsystemRegistry.get("bp_monitor").disabled is False


def test_bp_connect_exhausts_and_disables(provider, monkeypatch):
    factory = FakeSerialFactory(fail_times=99)
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    # connect() propagates serial.SerialException as-is (lib/bp_monitor.py is
    # left unchanged per the plan); retry_with_notify re-raises the original
    # exception type once attempts are exhausted, it doesn't wrap it.
    with pytest.raises(serial.SerialException):
        provider.measure_blood_pressure()

    assert factory.calls == 3
    assert SubsystemRegistry.get("bp_monitor").disabled is True


def test_bp_measure_timeout_is_not_retried(provider, monkeypatch):
    """Connect succeeds but the device never reports a result/READY line
    (simulated timeout). measure_blood_pressure should raise once, with no
    extra connect-retry attempts — only the connect step is retried, not a
    timed-out measurement (which can already take up to 120s)."""
    factory = FakeSerialFactory(fail_times=0, lines=[])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    import lib.bp_monitor as bp_monitor_module
    monkeypatch.setattr(bp_monitor_module.BPMonitor, "measure", lambda self, blocking=True: None)

    with pytest.raises(RuntimeError):
        provider.measure_blood_pressure()

    assert factory.calls == 1
    assert SubsystemRegistry.get("bp_monitor").disabled is False


def test_resolve_bp_port_honors_configured_when_present(provider, monkeypatch):
    provider.bp_port = "/dev/ttyUSB0"
    monkeypatch.setattr(
        "serial.tools.list_ports.comports", lambda: [_fake_port("/dev/ttyUSB0")]
    )
    assert provider._resolve_bp_port() == "/dev/ttyUSB0"


def test_resolve_bp_port_auto_detects_when_configured_is_stale(provider, monkeypatch):
    provider.bp_port = "/dev/ttyUSB0"  # set in .env but the number has shifted
    monkeypatch.setattr(
        "serial.tools.list_ports.comports", lambda: [_fake_port("/dev/ttyUSB1")]
    )
    assert provider._resolve_bp_port() == "/dev/ttyUSB1"


def test_resolve_bp_port_prefers_esp32_bridge_when_several_usb_serial(provider, monkeypatch):
    provider.bp_port = ""
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [
            _fake_port("/dev/ttyUSB0", vid=0x2341),  # some other USB serial
            _fake_port("/dev/ttyUSB1", vid=0x10C4),  # CP210x ESP32 bridge -> pick
        ],
    )
    assert provider._resolve_bp_port() == "/dev/ttyUSB1"


def test_resolve_bp_port_falls_back_to_sole_usb_serial(provider, monkeypatch):
    provider.bp_port = ""
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [_fake_port("/dev/ttyUSB0", vid=0x9999)],  # unknown chip, sole USB
    )
    assert provider._resolve_bp_port() == "/dev/ttyUSB0"


def test_resolve_bp_port_raises_when_no_usb_serial_present(provider, monkeypatch):
    provider.bp_port = ""
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [_fake_port("/dev/ttyS0", vid=None)],  # built-in UART, not USB
    )
    with pytest.raises(RuntimeError):
        provider._resolve_bp_port()
