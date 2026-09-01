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


def test_bp_progress_starts_when_driver_sends_start_not_while_connecting(provider, monkeypatch):
    factory = FakeSerialFactory(fail_times=0, lines=["SYS:120,DIA:80,PUL:70"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)
    seen = []
    provider.on_measurement_progress = lambda kind, value, state: seen.append(
        (kind, value, state, list(factory.ports[-1].writes))
    )

    provider.measure_blood_pressure()

    assert seen == [("bp", None, {"started": True}, ["STATUS", "RESET", "START"])]


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
    RESET puts it back to IDLE first -- but only once STATUS has gone
    unanswered, which is how firmware without either command behaves."""
    factory = FakeSerialFactory(fail_times=0, lines=["SYS:120,DIA:80,PUL:70", "READY"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    provider.measure_blood_pressure()

    assert factory.ports[-1].writes == ["STATUS", "RESET", "START"]


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


# ── STATUS before RESET ───────────────────────────────────────────────────

@pytest.fixture
def status_provider(provider, monkeypatch):
    """Same provider, but with a boot settle long enough for STATUS to be
    answered. The base fixture zeroes it, which is the no-answer path."""
    monkeypatch.setattr(cp.RealCareKeeperProvider, "_BP_BOOT_SETTLE_SECONDS", 0.5)
    return provider


def test_bp_skips_the_reset_when_the_firmware_says_it_is_idle(status_provider, monkeypatch):
    """RESET is a recovery step, not part of every measurement. Firmware that
    answers STATUS can say it is free, and then there is nothing to recover."""
    factory = FakeSerialFactory(
        fail_times=0,
        lines=["SYS:120,DIA:80,PUL:70", "READY"],
        status_reply="STATE:IDLE",
    )
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    result = status_provider.measure_blood_pressure()

    assert result.systolic == 120
    assert factory.ports[-1].writes == ["STATUS", "START"]


def test_bp_leaves_a_running_module_alone_instead_of_resetting_it(status_provider, monkeypatch):
    """The bug this closes: RESET rewinds the ESP32 while the cuff itself is
    still powering down, so the bridge reports READY and START then presses the
    module's button mid-cycle. Asking first turns that into an immediate,
    specific message and never touches the module at all."""
    factory = FakeSerialFactory(
        fail_times=0,
        lines=["SYS:120,DIA:80,PUL:70", "READY"],
        status_reply="STATE:WAIT_SHUTDOWN",
    )
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        status_provider.measure_blood_pressure()

    assert "ปิดตัวเอง" in str(excinfo.value)
    assert status_provider.last_bp_error == "NOT_READY"
    # Neither command was sent: no state was cleared and no button was pressed.
    assert factory.ports[-1].writes == ["STATUS"]
    assert SubsystemRegistry.get("bp_monitor").disabled is False


def test_bp_measuring_state_gets_its_own_wait_advice(status_provider, monkeypatch):
    factory = FakeSerialFactory(fail_times=0, lines=[], status_reply="STATE:MEASURING")
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        status_provider.measure_blood_pressure()

    assert "กำลังวัดรอบก่อนหน้า" in str(excinfo.value)


# ── a run that ends without a reading ─────────────────────────────────────

def test_bp_run_that_ends_empty_is_not_reported_as_no_response(provider, monkeypatch):
    """The cuff finished its cycle and only the reading went missing. Reporting
    that as "ไม่ตอบสนอง" sent the operator to check cables that were fine, and
    hid the fact that the patient had just sat through a full measurement."""
    factory = FakeSerialFactory(fail_times=0, lines=["READY"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_blood_pressure()

    assert "วัดครบรอบแล้วแต่ไม่ได้ส่งค่ากลับมา" in str(excinfo.value)
    assert provider.last_bp_error == "NO_RESULT"
    assert SubsystemRegistry.get("bp_monitor").disabled is False


def test_bp_firmware_no_result_line_reports_the_same_thing(provider, monkeypatch):
    """Reflashed firmware says it outright instead of leaving the host to infer
    it from a READY with nothing before it."""
    factory = FakeSerialFactory(fail_times=0, lines=["NO_RESULT"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_blood_pressure()

    assert "วัดครบรอบแล้วแต่ไม่ได้ส่งค่ากลับมา" in str(excinfo.value)
    assert provider.last_bp_error == "NO_RESULT"


# ── the module's own error code reaches the operator ──────────────────────

def test_bp_unmapped_device_code_is_shown_as_a_number(provider, monkeypatch):
    """Nothing is guessed about a code we have not observed, but the number
    still goes on screen -- it is what turns "it failed again" into a report
    someone can act on, and it is how the table gets filled in."""
    factory = FakeSerialFactory(fail_times=0, lines=["BP_ERROR:3"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_blood_pressure()

    assert "รหัสจากเครื่องวัด: 3" in str(excinfo.value)
    assert provider.last_bp_error == "BP_ERROR"
    assert provider.last_bp_error_code == 3


def test_bp_mapped_device_code_replaces_the_generic_message(provider, monkeypatch):
    """-1 is the firmware's own: the module went silent and its watchdog cut
    the run. Telling the operator to wait two minutes for a cuff lockout that
    never happened would send them to the wrong place."""
    factory = FakeSerialFactory(fail_times=0, lines=["BP_ERROR:-1"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_blood_pressure()

    message = str(excinfo.value)
    assert "หยุดตอบระหว่างวัด" in message
    assert "2 นาที" not in message
    assert provider.last_bp_error_code == -1


def test_bp_bare_device_error_message_is_unchanged(provider, monkeypatch):
    """Boards that have not been reflashed still send a bare BP_ERROR, and must
    still get the plain two-minute advice with no dangling code."""
    factory = FakeSerialFactory(fail_times=0, lines=["BP_ERROR"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_blood_pressure()

    message = str(excinfo.value)
    assert "2 นาที" in message
    assert "รหัส" not in message
    assert provider.last_bp_error_code is None


def test_bp_success_clears_a_code_left_by_an_earlier_failure(provider, monkeypatch):
    factory = FakeSerialFactory(fail_times=0, lines=["BP_ERROR:3"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)
    with pytest.raises(RuntimeError):
        provider.measure_blood_pressure()

    factory = FakeSerialFactory(fail_times=0, lines=["SYS:120,DIA:80,PUL:70", "READY"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)
    provider.measure_blood_pressure()

    assert provider.last_bp_error is None
    assert provider.last_bp_error_code is None


@pytest.mark.parametrize(
    "code, expected_phrases",
    [
        # 4 answered all four cuff-pressure failures we could stage: no
        # cuff, loose, wrapped hard, hose off. The message names both
        # directions and the hose -- an earlier version said only "loose,
        # wrap it tighter", which sent an operator whose cuff was already
        # too tight to make it tighter still.
        (4, ["หลวม", "แน่นเกินไป", "สายลม"]),
        (6, ["ขยับแขน"]),
    ],
)
def test_bp_observed_device_codes_name_the_cause(provider, monkeypatch, code, expected_phrases):
    """Codes 4 and 6 were produced deliberately on the kiosk and logged, so
    these two say what to check instead of showing a bare number -- without
    claiming more than the code can actually tell apart."""
    factory = FakeSerialFactory(fail_times=0, lines=[f"BP_ERROR:{code}"])
    monkeypatch.setattr("lib.bp_monitor.serial.Serial", factory)

    with pytest.raises(RuntimeError) as excinfo:
        provider.measure_blood_pressure()

    message = str(excinfo.value)
    for phrase in expected_phrases:
        assert phrase in message
    # A named cause replaces the number; showing both would just be noise.
    assert "รหัสจากเครื่องวัด" not in message
    assert provider.last_bp_error_code == code
