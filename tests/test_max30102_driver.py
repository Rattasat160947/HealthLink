# -*- coding: utf-8 -*-
"""Unit tests for the MAX30102 I2C driver (lib/spo2_max30102/max30102.py).

The module imports `smbus` at import time, so a fake bus is injected before
importing it. The fake models the three registers the read path depends on --
the two FIFO pointers and the overflow counter -- because the failure these
tests exist for is a disagreement between them: a FIFO that is full reads as
one that is empty.
"""
from __future__ import annotations

import importlib
import sys
import time
import types

import pytest


_MODULES = ("lib.spo2_max30102", "lib.spo2_max30102.max30102")


class FakeBus:
    """Enough of smbus.SMBus to drive the FIFO read path.

    `samples` is how many the FIFO is holding; reads pull from it. `overflow`
    is the dropped-sample counter. Writes are recorded so a FIFO restart can
    be asserted."""

    def __init__(self, samples=0, overflow=0):
        self.samples = samples
        self.overflow = overflow
        self.writes: list[tuple[int, list[int]]] = []
        self.part_id = 0x15

    def read_byte_data(self, address, register):
        if register == 0x05:      # REG_OVF_COUNTER
            return self.overflow
        if register == 0x04:      # REG_FIFO_WR_PTR
            return self.samples % 32
        if register == 0x06:      # REG_FIFO_RD_PTR
            return 0
        if register == 0xFF:      # REG_PART_ID
            return self.part_id
        return 0

    def read_i2c_block_data(self, address, register, length):
        if register == 0x07:      # REG_FIFO_DATA
            self.samples -= 1
            return [0x01, 0x00, 0x00, 0x02, 0x00, 0x00]
        return [0] * length

    def write_i2c_block_data(self, address, register, values):
        self.writes.append((register, list(values)))
        if register == 0x04:      # a restart parks the write pointer at 0
            self.samples = 0


@pytest.fixture
def driver_module(monkeypatch):
    fake_smbus = types.ModuleType("smbus")
    fake_smbus.SMBus = lambda channel: None
    monkeypatch.setitem(sys.modules, "smbus", fake_smbus)
    for name in _MODULES:
        sys.modules.pop(name, None)
    module = importlib.import_module("lib.spo2_max30102.max30102")
    yield module
    for name in _MODULES:
        sys.modules.pop(name, None)


def build(module, bus, **kwargs):
    """A MAX30102 wired to `bus`, skipping __init__'s reset/setup (which
    sleeps a second and writes a dozen registers we are not testing here)."""
    sensor = module.MAX30102.__new__(module.MAX30102)
    sensor.address = 0x57
    sensor.channel = 1
    sensor.bus = bus
    sensor.last_read_timed_out = False
    for key, value in kwargs.items():
        setattr(sensor, key, value)
    return sensor


def test_a_full_fifo_reads_as_empty_and_is_restarted(driver_module):
    """The bug that hung the CLI: rollover is off, so a full FIFO stops
    accepting samples with its write pointer parked exactly 32 ahead of the
    read pointer -- the same value as empty in 5-bit arithmetic. Waiting for
    it to drain waits for ever, because nothing drains it but this read."""
    bus = FakeBus(samples=32, overflow=4)  # 32 % 32 == 0: "empty", but full
    sensor = build(driver_module, bus)

    def restart_then_deliver(address, register, values):
        FakeBus.write_i2c_block_data(bus, address, register, values)
        if register == 0x06:      # REG_FIFO_RD_PTR, the second half of the
            bus.samples = 5       # restart -- the sensor starts filling again
            bus.overflow = 0

    bus.write_i2c_block_data = restart_then_deliver

    red, ir = sensor.read_sequential(5)

    assert len(red) == len(ir) == 5
    assert sensor.last_read_timed_out is False
    assert (0x04, [0x00]) in bus.writes and (0x06, [0x00]) in bus.writes


def test_a_restart_leaves_the_overflow_counter_for_the_caller(driver_module):
    """The counter is how the settling loop knows the next window spans a gap
    and has to be discarded, so restarting the FIFO must not clear it."""
    bus = FakeBus(samples=32, overflow=4)
    sensor = build(driver_module, bus, STALL_TIMEOUT_SECONDS=0.0)

    sensor.restart_fifo()

    assert bus.overflow == 4
    assert (0x05, [0x00]) not in bus.writes  # REG_OVF_COUNTER untouched


def test_a_silent_sensor_returns_short_instead_of_hanging(driver_module):
    """A sensor that never fills its FIFO used to spin here for ever, taking
    the settling loop's own deadline with it -- the loop only checks the clock
    between reads."""
    bus = FakeBus(samples=0, overflow=0)
    sensor = build(driver_module, bus, STALL_TIMEOUT_SECONDS=0.05)

    red, ir = sensor.read_sequential(100)

    assert red == [] and ir == []
    assert sensor.last_read_timed_out is True


def test_the_silence_timer_restarts_whenever_samples_arrive(driver_module):
    """100 samples take 4 s at the configured rate, so the timeout has to be
    'nothing arrived for a while', not a budget for the whole read."""
    bus = FakeBus(samples=0)
    sensor = build(driver_module, bus, STALL_TIMEOUT_SECONDS=0.2)
    delivered = {"n": 0}

    def trickle(address, register):
        # one sample appears on every pointer check, slower than the timeout
        if register == 0x04 and delivered["n"] < 6:
            delivered["n"] += 1
            bus.samples += 1
        return FakeBus.read_byte_data(bus, address, register)

    bus.read_byte_data = trickle

    red, _ = sensor.read_sequential(6)

    assert len(red) == 6
    assert sensor.last_read_timed_out is False


def test_a_normal_read_drains_the_batch_it_started(driver_module):
    """Unchanged behaviour: a batch already in the FIFO is drained whole even
    if that overshoots `amount` -- leaving samples behind is what fills the
    FIFO up. The caller trims the window (see _slide_window)."""
    bus = FakeBus(samples=10)
    sensor = build(driver_module, bus)

    red, ir = sensor.read_sequential(4)

    assert len(red) == len(ir) == 10
    assert bus.writes == []          # nothing restarted
    assert sensor.last_read_timed_out is False


def test_an_unexpected_part_id_is_reported_not_fatal(driver_module, capsys):
    """A MAX30100 (different register map) presents as a FIFO that never
    fills. Worth saying at startup -- but not worth refusing to run over, in
    case a working clone answers something else."""
    bus = FakeBus()
    bus.part_id = 0x11
    sensor = build(driver_module, bus)

    sensor.setup()

    assert "0x11" in capsys.readouterr().out


def test_a_missing_part_id_does_not_raise(driver_module):
    bus = FakeBus()

    def boom(address, register):
        raise OSError("no ack")

    bus.read_byte_data = boom
    sensor = build(driver_module, bus)

    assert sensor.read_part_id() == -1


# ── flushing between windows ──────────────────────────────────────────────

def test_flushing_a_readable_fifo_drains_it_without_writing_a_pointer(driver_module):
    """The regression that took SpO2 out entirely.

    An earlier version of this method zeroed the pointers -- the datasheet's
    INIT sequence -- and _clear_window() calls it on every window clear, so it
    ran against a part already mid-acquisition. Draining through the ordinary
    read path instead cannot leave the part in a state it does not expect,
    because it is the path the part is in the middle of anyway."""
    bus = FakeBus(samples=14)
    build(driver_module, bus).flush_fifo()

    assert bus.samples == 0                       # read out and thrown away
    written = {reg for reg, _ in bus.writes}
    assert 0x04 not in written and 0x06 not in written
    assert 0x05 in written                        # overflow count cleared


def test_flushing_a_full_fifo_restarts_it(driver_module):
    """The one case where writing a pointer here is right: a full FIFO reads
    as an empty one, so there is nothing to drain and nothing but a restart
    clears it."""
    bus = FakeBus(samples=32, overflow=3)
    sensor = build(driver_module, bus)
    assert sensor.get_data_present() == 0         # full, and it looks empty

    sensor.flush_fifo()

    written = {reg for reg, _ in bus.writes}
    assert 0x04 in written and 0x06 in written


def test_flushing_an_empty_fifo_touches_no_pointer(driver_module):
    bus = FakeBus(samples=0)
    build(driver_module, bus).flush_fifo()

    assert {reg for reg, _ in bus.writes} == {0x05}


def test_a_fifo_that_stays_full_still_gives_up(driver_module):
    """A restart is tried once and then the silence timer takes over.

    restart_fifo() leaves the overflow counter set on purpose, so it still
    reads true the instant after a restart. Treating that as "restart again"
    spins on three I2C transactions per pass without ever reaching the
    deadline -- the same hang the timeout exists to prevent, reached through
    a different door. Verified against a part that is full and has stopped
    producing samples, which is the state that door opens onto."""
    class FullForeverBus(FakeBus):
        def read_byte_data(self, address, register):
            if register == 0x05:      # overflow, never cleared by a restart
                return 3
            return 0                  # write and read pointers stay equal

    bus = FullForeverBus()
    sensor = build(driver_module, bus, STALL_TIMEOUT_SECONDS=0.05)

    red, ir = sensor.read_sequential(100)

    assert red == [] and ir == []
    assert sensor.last_read_timed_out is True
    restarts = [reg for reg, _ in bus.writes if reg == 0x04]
    assert len(restarts) == 1          # tried once, not spun on


def test_a_trickling_sensor_cannot_outlive_the_callers_budget(driver_module):
    """The silence timer restarts every time samples arrive, so a part that
    keeps producing them -- just far slower than setup() asked for -- resets it
    for ever and this call never ends. measure_spo2() tests its own deadline
    only BETWEEN reads, so its 30 s budget is worth no more than the longest
    single read is bounded."""
    class TricklingBus(FakeBus):
        def read_byte_data(self, address, register):
            return 1 if register == 0x04 else 0     # one sample, always
        def read_i2c_block_data(self, address, register, length):
            return [0x01, 0x00, 0x00, 0x02, 0x00, 0x00]

    sensor = build(driver_module, TricklingBus(), MAX_READ_SECONDS=0.05)

    started = time.monotonic()
    red, ir = sensor.read_sequential(10_000_000)

    assert time.monotonic() - started < 2.0
    assert len(red) < 10_000_000
    assert sensor.last_read_timed_out is True
