# -*- coding: utf-8 -*-
"""MAX30102 driver behaviour that the settling loop above it depends on.

The driver imports `smbus` at module level, so a fake is injected before it is
imported and the sensor is built without running __init__ (which resets the
part and sleeps a second). What is under test is the FIFO handling: the two
places where this driver can take a whole measurement down with it.
"""
from __future__ import annotations

import importlib
import sys
import time
import types

import pytest


REG_FIFO_WR_PTR = 0x04
REG_OVF_COUNTER = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA = 0x07


@pytest.fixture
def driver(monkeypatch):
    fake_smbus = types.ModuleType("smbus")
    fake_smbus.SMBus = object
    monkeypatch.setitem(sys.modules, "smbus", fake_smbus)
    sys.modules.pop("lib.spo2_max30102.max30102", None)
    module = importlib.import_module("lib.spo2_max30102.max30102")
    yield module
    sys.modules.pop("lib.spo2_max30102.max30102", None)


class StalledBus:
    """A part whose write pointer never advances: no sample ever arrives.

    However it got there -- powered down, wedged, pointers written out from
    under its own full/empty tracking -- this is what the driver sees."""

    def __init__(self, wr=0, rd=0):
        self.regs = {REG_FIFO_WR_PTR: wr, REG_FIFO_RD_PTR: rd, REG_OVF_COUNTER: 0}
        self.writes = []
        self.sample_reads = 0

    def read_byte_data(self, addr, reg):
        return self.regs.get(reg, 0)

    def read_i2c_block_data(self, addr, reg, length):
        if reg == REG_FIFO_DATA:
            self.sample_reads += 1
        return [0] * length

    def write_i2c_block_data(self, addr, reg, data):
        self.writes.append((reg, data[0]))
        self.regs[reg] = data[0]


class StreamingBus(StalledBus):
    """A healthy part: unread samples are always waiting, eight at a time.

    `total=None` streams forever; a number makes the part go quiet after that
    many samples, which is what a stall part way through a window looks like."""

    def __init__(self, total=None, **kwargs):
        super().__init__(**kwargs)
        self.remaining = total

    def _ahead(self):
        if self.remaining is None:
            return 8
        return min(8, max(0, self.remaining))

    def read_byte_data(self, addr, reg):
        if reg == REG_FIFO_WR_PTR:
            return (self.regs[REG_FIFO_RD_PTR] + self._ahead()) % 32
        return self.regs.get(reg, 0)

    def read_i2c_block_data(self, addr, reg, length):
        if reg == REG_FIFO_DATA:
            self.sample_reads += 1
            self.regs[REG_FIFO_RD_PTR] = (self.regs[REG_FIFO_RD_PTR] + 1) % 32
            if self.remaining is not None:
                self.remaining -= 1
            return [0x01, 0x02, 0x03, 0x04, 0x05, 0x06]
        return [0] * length


def sensor_on(driver, bus):
    """A MAX30102 bound to `bus`, skipping __init__'s reset and 1 s sleep."""
    sensor = driver.MAX30102.__new__(driver.MAX30102)
    sensor.address = 0x57
    sensor.channel = 1
    sensor.bus = bus
    return sensor


# ── a read that cannot hang the measurement ───────────────────────────────

def test_read_sequential_gives_up_when_the_fifo_stops_advancing(driver):
    """The regression this file exists for.

    With no way out of this loop, a part that stopped producing samples took
    the whole measurement with it: measure_spo2() tests its deadline at the top
    of its own loop, which a call that never returns never reaches. On the
    kiosk that is a measurement that hangs with no value, no error and no
    timeout -- it simply never reads."""
    sensor = sensor_on(driver, StalledBus())

    started = time.monotonic()
    red, ir = sensor.read_sequential(100, no_data_timeout=0.05)

    assert time.monotonic() - started < 2.0
    assert red == [] and ir == []


def test_a_stall_part_way_through_returns_what_was_read(driver):
    """A short window is already meaningful upstream (it is skipped as "not
    enough signal buffered yet"); a hang is not."""
    sensor = sensor_on(driver, StreamingBus(total=40))

    started = time.monotonic()
    red, ir = sensor.read_sequential(100, no_data_timeout=0.05)

    assert time.monotonic() - started < 2.0
    assert 0 < len(red) < 100
    assert len(red) == len(ir)


def test_a_healthy_part_still_reads_the_whole_block(driver):
    """At least `amount`, not exactly: this read drains whatever the FIFO
    holds once it has committed to a block, so it can overshoot slightly.
    _slide_window() trims by slicing for that reason."""
    sensor = sensor_on(driver, StreamingBus())

    red, ir = sensor.read_sequential(100)

    assert len(red) >= 100 and len(ir) == len(red)


# ── flushing without desyncing the part ───────────────────────────────────

def test_flush_drains_everything_the_fifo_holds(driver):
    bus = StalledBus(wr=17, rd=3)          # 14 samples waiting
    sensor_on(driver, bus).flush_fifo()

    assert bus.sample_reads == 14
    assert bus.regs[REG_OVF_COUNTER] == 0


def test_flush_never_writes_a_fifo_pointer(driver):
    """The regression that took SpO2 out entirely.

    Zeroing WR_PTR is the datasheet's INIT sequence, and issuing it against a
    part already mid-acquisition moves the pointers out from under the chip's
    own full/empty tracking -- after which a FIFO reading back WR == RD cannot
    be told apart from an empty one, and read_sequential() waits on samples
    that never come. Draining through the ordinary read path cannot do that,
    so no pointer may be written here at all."""
    bus = StalledBus(wr=17, rd=3)
    sensor_on(driver, bus).flush_fifo()

    written = {reg for reg, _ in bus.writes}
    assert REG_FIFO_WR_PTR not in written
    assert REG_FIFO_RD_PTR not in written
    assert bus.regs[REG_FIFO_WR_PTR] == 17


def test_flush_of_an_empty_fifo_reads_nothing(driver):
    bus = StalledBus(wr=9, rd=9)
    sensor = sensor_on(driver, bus)
    sensor.flush_fifo()

    assert bus.sample_reads == 0
    assert sensor.get_data_present() == 0
