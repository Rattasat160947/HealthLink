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
    # Skip the real ESP32 boot-settle wait; no bridge is booting behind a fake.
    monkeypatch.setattr(cp.RealCareKeeperProvider, "_BP_BOOT_SETTLE_SECONDS", 0)
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

    # connect() propagates the SerialException that open() raises; once the
    # attempts are exhausted retry_with_notify re-raises the original
    # exception type, it doesn't wrap it.
    with pytest.raises(serial.SerialException):
        provider.measure_blood_pressure()

    assert factory.calls == 3
    assert SubsystemRegistry.get("bp_monitor").disabled is True


def test_bp_lets_the_bridge_reboot_and_drops_its_boot_chatter(provider, monkeypatch):
    """DTR/RTS drive the ESP32's auto-reset circuit, so opening the port
    reboots the bridge — and that is kept on purpose, so each measurement
    starts from freshly booted firmware rather than whatever state the last
    one left. What must not happen is START arriving before the boot ends;
    connect() waits it out, and the boot chatter is flushed."""
    factory = FakeSerialFactory(fail_times=0, lines=["SYS:120,DIA:80,PUL:70", "READY"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    provider.measure_blood_pressure()

    port = factory.ports[-1]
    assert port.opened_with_dtr is True
    assert port.opened_with_rts is True
    assert port.reset_input_calls >= 1


def test_bp_reset_can_be_turned_off_for_slow_booting_firmware(monkeypatch):
    """Escape hatch: deasserting the handshake lines first keeps running
    firmware running. Off by default because a wedged firmware then stays
    wedged, but boards that are slow to come up need it."""
    from lib.bp_monitor import BPMonitor

    factory = FakeSerialFactory(fail_times=0, lines=[])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    monitor = BPMonitor(
        port="/dev/fake-port", boot_settle_seconds=0, reset_on_connect=False
    )
    monitor.connect()
    monitor.disconnect()

    port = factory.ports[-1]
    assert port.opened_with_dtr is False
    assert port.opened_with_rts is False


def test_bp_device_error_reports_the_two_minute_wait(provider, monkeypatch):
    """A BP_ERROR from the cuff must surface as its own message — the operator
    has to wait out the device's lockout, which "ไม่สามารถอ่านค่าความดันได้" never said."""
    factory = FakeSerialFactory(fail_times=0, lines=["BP_ERROR"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_blood_pressure()

    assert "2 นาที" in str(excinfo.value)
    # A device-reported fault is not a broken subsystem: the cuff answered.
    assert SubsystemRegistry.get("bp_monitor").disabled is False


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


def test_bp_clears_stale_firmware_state_before_measuring(provider, monkeypatch):
    """Opening the port does not reset this board, so the firmware's state
    machine survives the app being restarted. An interrupted run therefore kept
    every later START answered with NOT_READY until the module finished and
    powered down -- the two minutes operators had to wait after a restart.
    RESET puts it back to IDLE first."""
    factory = FakeSerialFactory(fail_times=0, lines=["SYS:120,DIA:80,PUL:70", "READY"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    provider.measure_blood_pressure()

    assert factory.ports[-1].writes == ["RESET", "START"]


def test_bp_not_ready_says_which_state_the_firmware_is_stuck_in(provider, monkeypatch):
    """"Still measuring" and "powering down" are both NOT_READY but need
    different advice, so the operator is told which wait they are in."""
    factory = FakeSerialFactory(fail_times=0, lines=["NOT_READY:WAIT_SHUTDOWN"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_blood_pressure()

    assert "ปิดตัวเอง" in str(excinfo.value)
    assert provider.last_bp_error == "NOT_READY"
    # The cuff answered, so nothing is broken about the subsystem.
    assert SubsystemRegistry.get("bp_monitor").disabled is False


def test_bp_not_ready_falls_back_when_firmware_sends_no_state(provider, monkeypatch):
    """Boards that have not been reflashed still send a bare NOT_READY."""
    factory = FakeSerialFactory(fail_times=0, lines=["NOT_READY"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_blood_pressure()

    assert "รอบก่อนหน้าไม่เสร็จ" in str(excinfo.value)


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
