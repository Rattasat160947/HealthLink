# -*- coding: utf-8 -*-
from __future__ import annotations

import serial


class FakeSerial:
    """Fake pyserial.Serial replacement for BPMonitor tests. Scripted
    response lines only become readable once a "START" command is written,
    mirroring real hardware timing — BPMonitor's background read thread
    starts as soon as connect() returns, so if lines were available
    immediately it could process (and the subsequent measure() call could
    clear) a result before the test ever sends START.

    BPMonitor now constructs the port unopened and calls open() itself (so it
    can deassert DTR/RTS first and avoid resetting the ESP32 bridge), so this
    fake models that two-step flow: `fail_open` makes open() raise the way a
    missing port does."""

    def __init__(self, lines=None, fail_open: bool = False):
        self.is_open = False
        self.fail_open = fail_open
        self.opened_with_dtr = None
        self.opened_with_rts = None
        self.reset_input_calls = 0
        # Set by BPMonitor before open(); kept so tests can assert on them.
        self.port = None
        self.baudrate = None
        self.timeout = None
        self.dtr = True
        self.rts = True
        self._pending_lines = list(lines or [])
        self._available_lines: list[str] = []
        self.in_waiting = 0

    def open(self) -> None:
        if self.fail_open:
            raise serial.SerialException("fake port not found")
        # Snapshot the handshake lines as they stood at open time: that is the
        # moment the auto-reset circuit would fire on real hardware.
        self.opened_with_dtr = self.dtr
        self.opened_with_rts = self.rts
        self.is_open = True

    def reset_input_buffer(self) -> None:
        self.reset_input_calls += 1

    def write(self, data: bytes) -> None:
        text = data.decode(errors="ignore").strip()
        if text == "START":
            self._available_lines = self._pending_lines
            self._pending_lines = []
            self.in_waiting = 1 if self._available_lines else 0

    def readline(self) -> bytes:
        if self._available_lines:
            line = self._available_lines.pop(0)
            self.in_waiting = 1 if self._available_lines else 0
            return (line + "\n").encode()
        return b""

    def close(self) -> None:
        self.is_open = False


class FakeSerialFactory:
    """Callable replacement for `serial.Serial()`. The port it hands back
    fails to open for the first `fail_times` calls, then opens normally and
    serves `lines`. `calls` counts constructions, one per connect() attempt."""

    def __init__(self, fail_times: int = 0, lines=None):
        self.fail_times = fail_times
        self.lines = lines or []
        self.calls = 0
        self.ports: list[FakeSerial] = []

    def __call__(self, *args, **kwargs):
        self.calls += 1
        port = FakeSerial(
            lines=list(self.lines),
            fail_open=self.calls <= self.fail_times,
        )
        self.ports.append(port)
        return port
