# -*- coding: utf-8 -*-
"""Unit tests for the SpO2 settling loop (lib/spo2_max30102/spo2_monitor.py).

The module imports `smbus` (Raspberry Pi I2C) through max30102.py at import
time, so a fake smbus module is injected before importing it, and the monitor
is built with an injected fake sensor -- no I2C, no MAX30102, no waiting.
`calc_hr_and_spo2` is replaced with a scripted sequence so each test controls
exactly which SpO2 values the algorithm "produces"; what is under test is the
settling logic on top of it, not the DSP.
"""
from __future__ import annotations

import importlib
import sys
import time
import types

import pytest


_MODULES = (
    "lib.spo2_max30102",
    "lib.spo2_max30102.spo2_monitor",
    "lib.spo2_max30102.max30102",
)


@pytest.fixture
def spo2_module(monkeypatch):
    fake_smbus = types.ModuleType("smbus")
    fake_smbus.SMBus = object
    monkeypatch.setitem(sys.modules, "smbus", fake_smbus)
    for name in _MODULES:
        sys.modules.pop(name, None)
    module = importlib.import_module("lib.spo2_max30102.spo2_monitor")
    yield module
    for name in _MODULES:
        sys.modules.pop(name, None)


class FakeSensor:
    """Stand-in for MAX30102.

    Returns flat sample blocks whose IR level is scripted per read (that DC
    level is what the finger check looks at) and records how many samples each
    read asked for, so the sliding window can be asserted."""

    def __init__(self, ir_levels=(80000,)):
        self.ir_levels = list(ir_levels)
        self.requested: list[int] = []

    def read_sequential(self, amount=110):
        level = self.ir_levels[min(len(self.requested), len(self.ir_levels) - 1)]
        self.requested.append(amount)
        return [level // 2] * amount, [level] * amount


def script_calc(monkeypatch, module, values, bpm=72):
    """Make calc_hr_and_spo2 return `values` in order (last value repeats).

    None means "the algorithm could not compute this window" (invalid flags),
    which is what a motion artifact or weak perfusion looks like."""
    calls = {"n": 0, "lengths": []}

    def fake_calc(ir_data, red_data):
        sp = values[min(calls["n"], len(values) - 1)]
        calls["n"] += 1
        calls["lengths"].append(len(ir_data))
        if sp is None:
            return -999, False, -999, False
        return bpm, True, float(sp), True

    monkeypatch.setattr(module, "calc_hr_and_spo2", fake_calc)
    return calls


def quiet(spo2, **kwargs):
    """on_progress=None means "print" (the library default), so tests that do
    not inspect progress pass this instead of leaving the loop printing."""


def build(module, sensor, **kwargs):
    kwargs.setdefault("max_wait_seconds", 5)
    return module.SpO2Monitor(sensor=sensor, **kwargs)


def test_returns_median_of_a_settled_window(spo2_module, monkeypatch):
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [97, 98, 97, 98, 97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97


def test_window_fills_once_then_slides_one_second_at_a_time(spo2_module, monkeypatch):
    """A fresh 100-sample read per estimate would cost ~4 s each (~20 s for a
    measurement); the window is filled once and then advanced 25 samples (1 s)
    per estimate, which is also why five readings span more than 100 samples
    and are therefore independent of each other."""
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    monitor.measure_spo2(on_progress=quiet)

    assert sensor.requested == [100, 25, 25, 25, 25]
    assert monitor.stability_window * spo2_module.SpO2Monitor.STEP_SAMPLES >= 100


def test_keeps_measuring_until_the_values_agree(spo2_module, monkeypatch):
    """The old code returned the first in-range sample (90 here). Now the
    jittery run has to settle first, so the result is the settled 97."""
    sensor = FakeSensor()
    calls = script_calc(monkeypatch, spo2_module, [90, 99, 95, 97, 97, 97, 97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    # Six, not seven: by the sixth window the outlying 90 has slid out and the
    # remaining spread is within threshold once the one deviant reading is
    # trimmed (see _trimmed_spread). Not one -- it still did not stop at the
    # first valid value, which is what this test exists to hold.
    assert calls["n"] == 6


def test_out_of_range_and_uncomputable_windows_are_skipped(spo2_module, monkeypatch):
    """40% / 101% are unphysiological and None is a window the algorithm could
    not compute; none of them may enter the stability window (if any did, the
    spread would keep the run from ever settling on 97)."""
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [40, 101, None, 97, 97, 97, 97, 97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97


def test_no_finger_never_settles(spo2_module, monkeypatch):
    """IR DC level far below the threshold = nothing on the sensor. It times
    out instead of reporting a value, and every progress callback says so."""
    sensor = FakeSensor(ir_levels=(1200,))
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor, max_wait_seconds=0.15)

    seen = []
    result = monitor.measure_spo2(
        on_progress=lambda spo2, **kw: seen.append((spo2, kw["finger_detected"]))
    )

    assert result is None
    assert seen and all(value is None and not finger for value, finger in seen)


def test_lifting_the_finger_restarts_the_measurement(spo2_module, monkeypatch):
    """Four good windows, then the finger comes off for one read, then a run at
    a different level. Progress must be dropped on the lift, so the result is
    the post-lift value (95), never a mix of before and after (97)."""
    sensor = FakeSensor(ir_levels=(80000, 80000, 80000, 80000, 1200, 80000))
    script_calc(monkeypatch, spo2_module, [97, 97, 97, 97, 95, 95, 95, 95, 95])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 95
    # the window was refilled from scratch after the lift, not slid on top of
    # samples that straddle the gap
    assert sensor.requested.count(100) == 2


def test_settled_measurement_exposes_the_matching_pulse(spo2_module, monkeypatch):
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [97, 98, 97, 98, 97], bpm=74)
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert monitor.spo2 == 97
    assert monitor.bpm == 74


def test_progress_reports_each_reading_then_the_stable_one(spo2_module, monkeypatch):
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [96, 97, 96, 97, 96])
    monitor = build(spo2_module, sensor)

    seen = []
    monitor.measure_spo2(on_progress=lambda spo2, **kw: seen.append((spo2, kw["stable"])))

    assert seen == [(96, False), (97, False), (96, False), (97, False), (96, True)]


def test_get_spo2_sensor_still_returns_an_instantaneous_reading(spo2_module, monkeypatch):
    """The raw one-shot API is unchanged -- lib/spo2_max30102/spo2_monitor.py
    --raw and any debugging code keep working."""
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [95], bpm=80)
    monitor = build(spo2_module, sensor)

    bpm, spo2, red, ir = monitor.GetSpO2Sensor()

    assert (bpm, spo2) == (80, 95)
    assert len(red) == len(ir) == 110


def test_a_partly_filled_window_is_never_measured(spo2_module, monkeypatch):
    """calc_hr_and_spo2() indexes a fixed 100-sample window, so handing it a
    short one would read past the end of the array. A FIFO that returns fewer
    samples than asked must therefore only delay the first estimate -- the
    algorithm still only ever sees a full window."""

    class ShortSensor(FakeSensor):
        """Returns 40 samples per read however many were asked for."""

        def read_sequential(self, amount=110):
            red, ir = super().read_sequential(amount)
            return red[:40], ir[:40]

    sensor = ShortSensor()
    calls = script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert calls["lengths"] and all(n == 100 for n in calls["lengths"])
    # 40 + 40 + 40 -> the first two reads are too short to measure on
    assert len(sensor.requested) == calls["n"] + 2


def test_over_long_reads_do_not_grow_the_window(spo2_module, monkeypatch):
    """read_sequential() drains whatever the FIFO holds, so it can hand back a
    few more samples than requested; the extra must be trimmed away."""

    class GreedySensor(FakeSensor):
        def read_sequential(self, amount=110):
            return super().read_sequential(amount + 7)

    calls = script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, GreedySensor())

    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert all(n == 100 for n in calls["lengths"])


def test_no_samples_means_no_finger(spo2_module):
    monitor = build(spo2_module, FakeSensor())
    assert monitor.is_finger_present([]) is False


def test_default_progress_prints_each_state(spo2_module, capsys):
    """Running the module by hand on the Pi is the field-debugging path, so the
    printed states have to distinguish contact / weak signal / settled."""
    progress = spo2_module.SpO2Monitor.default_progress

    progress(None, bpm=0, stable=False, finger_detected=False)
    progress(None, bpm=70, stable=False, finger_detected=True)
    progress(97, bpm=70, stable=False, finger_detected=True)
    progress(97, bpm=70, stable=True, finger_detected=True)
    lines = capsys.readouterr().out.splitlines()

    assert "Place a finger" in lines[0]
    assert "weak signal" in lines[1]
    assert "measuring..." in lines[2]
    assert "SpO2 97%" in lines[3] and "stable" in lines[3]


def test_progress_defaults_to_printing(spo2_module, monkeypatch, capsys):
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2() == 97
    assert "stable" in capsys.readouterr().out


# ── FIFO gaps ─────────────────────────────────────────────────────────────

class OverflowingSensor(FakeSensor):
    """FakeSensor that reports dropped samples after chosen reads.

    A real MAX30102 stops accepting samples once its 32-deep FIFO fills, so
    whatever is read next is contiguous in memory but has a hole in time."""

    def __init__(self, overflow_after=(), **kwargs):
        super().__init__(**kwargs)
        self.overflow_after = set(overflow_after)
        self.cleared = 0

    def get_overflow_count(self):
        return 7 if len(self.requested) in self.overflow_after else 0

    def clear_overflow_count(self):
        self.cleared += 1


def test_a_window_spanning_a_fifo_gap_is_discarded(spo2_module, monkeypatch):
    """The samples either side of a dropped-sample gap are seconds apart with
    nothing marking the seam, so peak intervals and the AC/DC ratio across it
    are wrong in a way nothing downstream can detect. The window has to be
    refilled from scratch, exactly as it is after a lifted finger."""
    sensor = OverflowingSensor(overflow_after=(2,))
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    # read 1 fills the window, read 2 slides it and then overflows, so read 3
    # must fill from scratch rather than slide over the seam
    assert sensor.requested[:3] == [100, 25, 100]
    assert monitor.overflows == 1
    # At least once, not exactly once: clearing the window now also empties
    # the sensor's FIFO (see _reset_fifo), which clears the counter on this
    # older fake as well. What matters is that the gap is not left standing to
    # condemn the window read after it.
    assert sensor.cleared >= 1


def test_a_clean_run_reports_no_gaps(spo2_module, monkeypatch):
    sensor = OverflowingSensor()
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert monitor.overflows == 0


class BufferingSensor(FakeSensor):
    """FakeSensor whose FIFO is already full and overflowing before the run.

    This is the state a real MAX30102 is always in by the time a measurement
    starts: it samples continuously from setup(), and with rollover off its
    32-deep FIFO fills in 1.28 s -- while the provider is still constructing
    the monitor and the person is still being told to place a finger."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.overflowing = True   # stale, from the idle time before the run
        self.fifo_flushes = 0

    def get_overflow_count(self):
        return 7 if self.overflowing else 0

    def clear_overflow_count(self):
        self.overflowing = False

    def flush_fifo(self):
        self.fifo_flushes += 1
        self.overflowing = False


def test_a_stale_overflow_does_not_cost_the_first_window(spo2_module, monkeypatch):
    """An overflow that happened before the measurement began says nothing
    about the samples read after it.

    Without emptying the FIFO first, the first window of EVERY run was built
    from samples taken before the finger arrived and was then discarded on the
    strength of that idle overflow -- ~4 s of a 30 s budget, gone every time,
    before the person on the sensor had done anything wrong."""
    sensor = BufferingSensor()
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert sensor.fifo_flushes >= 1  # emptied before the first read
    assert monitor.overflows == 0    # so no window was thrown away
    assert sensor.requested == [100, 25, 25, 25, 25]  # filled once, then slid


def test_one_artifact_window_does_not_block_a_settled_run(spo2_module, monkeypatch):
    """91 among four 97s is a swallow or a knuckle shifting, not a real move:
    it is inside the physiological range so it enters the window, and on the
    raw max-min it would hold the run up for another five seconds."""
    sensor = FakeSensor()
    calls = script_calc(monkeypatch, spo2_module, [97, 97, 91, 97, 97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert calls["n"] == 5


def test_trimming_does_not_forgive_a_second_deviant_window(spo2_module):
    """Trimming one reading must not turn "the value keeps moving" into a
    result. One window off among four that agree is an artifact; two is a
    signal that has not settled, and the spread still has to say so."""
    trim = spo2_module.SpO2Monitor._trimmed_spread

    assert trim([97, 97, 91, 97, 97]) <= 3.0   # one artifact -- forgiven
    assert trim([92, 98, 92, 98, 92]) > 3.0    # swinging every window
    assert trim([97, 91, 97, 91, 97]) > 3.0


def test_sensors_without_an_overflow_counter_still_work(spo2_module, monkeypatch):
    """Injected sensors need not model the FIFO, so the check is optional."""
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert monitor.overflows == 0


# ── why it failed ─────────────────────────────────────────────────────────

def test_failure_reason_is_no_finger_when_contact_never_happened(spo2_module, monkeypatch):
    sensor = FakeSensor(ir_levels=(1200,))
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor, max_wait_seconds=0.15)

    assert monitor.measure_spo2(on_progress=quiet) is None
    assert monitor.last_error == spo2_module.SpO2Monitor.ERR_NO_FINGER
    # the observed level is kept so a mis-tuned threshold is visible
    assert monitor.last_ir_dc == 1200


def test_failure_reason_is_weak_signal_when_the_algorithm_got_nothing(spo2_module, monkeypatch):
    """Finger on the sensor, but every window is uncomputable — bad contact or
    poor perfusion, not bad placement. Telling those apart is the point."""
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [None])
    monitor = build(spo2_module, sensor, max_wait_seconds=0.15)

    assert monitor.measure_spo2(on_progress=quiet) is None
    assert monitor.last_error == spo2_module.SpO2Monitor.ERR_WEAK


def test_a_dark_signal_is_still_a_finger_problem(spo2_module, monkeypatch):
    """Samples ARE arriving here, they are just dark. That is placement, and
    the no-data branch above must not swallow it."""
    sensor = FakeSensor(ir_levels=(1200,))
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor, max_wait_seconds=0.15)

    assert monitor.measure_spo2(on_progress=quiet) is None
    assert monitor.last_error == spo2_module.SpO2Monitor.ERR_NO_FINGER
    assert monitor.stalled_reads == 0


def test_failure_reason_is_unstable_when_values_never_agree(spo2_module, monkeypatch):
    """Values keep arriving, they just never hold still. script_calc repeats
    its last value (which would settle), so alternate indefinitely instead."""
    sensor = FakeSensor()
    flip = {"n": 0}

    def alternating(ir_data, red_data):
        flip["n"] += 1
        return 72, True, (90.0 if flip["n"] % 2 else 99.0), True

    monkeypatch.setattr(spo2_module, "calc_hr_and_spo2", alternating)
    monitor = build(spo2_module, sensor, max_wait_seconds=0.15)

    assert monitor.measure_spo2(on_progress=quiet) is None
    assert monitor.last_error == spo2_module.SpO2Monitor.ERR_UNSTABLE


def test_a_settled_reading_clears_the_failure_reason(spo2_module, monkeypatch):
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert monitor.last_error is None


# ── per-unit calibration ──────────────────────────────────────────────────

def test_finger_threshold_reads_the_environment_at_import(monkeypatch):
    """Boards differ in how much IR a finger returns, so the cutoff has to be
    tunable on the kiosk without editing the library."""
    fake_smbus = types.ModuleType("smbus")
    fake_smbus.SMBus = object
    monkeypatch.setitem(sys.modules, "smbus", fake_smbus)
    monkeypatch.setenv("CAREKEEPER_SPO2_FINGER_IR_THRESHOLD", "25000")
    for name in _MODULES:
        sys.modules.pop(name, None)
    try:
        module = importlib.import_module("lib.spo2_max30102.spo2_monitor")
        assert module.SpO2Monitor.FINGER_IR_THRESHOLD == 25000
    finally:
        for name in _MODULES:
            sys.modules.pop(name, None)


def test_an_ir_level_that_used_to_read_as_no_finger_now_counts(spo2_module, monkeypatch):
    """Regression for the 50000 default: at the old cutoff this level was a
    finger the code refused to see, and the run could only ever time out."""
    sensor = FakeSensor(ir_levels=(30000,))
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97


# ── why a window was uncomputable ─────────────────────────────────────────

def window(dc=80000, ac=800, samples=100):
    """A plausible IR window: `ac` counts of pulse riding on `dc`."""
    import math

    return [int(dc + (ac / 2) * math.sin(2 * math.pi * i / 25)) for i in range(samples)]


def test_a_healthy_window_is_not_blamed_on_the_finger(spo2_module):
    """The classifier must stay quiet when the samples look fine -- otherwise
    a beat lost to movement would tell the patient to press lighter."""
    monitor = build(spo2_module, FakeSensor())
    ir = window()
    red = window(dc=40000, ac=400)

    assert monitor.assess_signal(red, ir) is None
    assert monitor.dominant_quality is None
    assert monitor.last_perfusion > spo2_module.SpO2Monitor.MIN_PERFUSION_INDEX


def test_clipped_samples_are_reported_as_saturation(spo2_module):
    """Pressed hard against a 12.6 mA LED the ADC parks at 0x3FFFF: the DC
    level looks excellent while the crests of the waveform are cut flat."""
    monitor = build(spo2_module, FakeSensor())
    ir = window(dc=260000, ac=8000)
    red = window(dc=130000, ac=4000)

    assert monitor.assess_signal(red, ir) == spo2_module.SpO2Monitor.QUALITY_SATURATED


def test_a_dark_red_channel_is_reported_as_partial_contact(spo2_module):
    """Red and IR sit side by side under one detector. A finger on one edge
    covers IR -- which is all the contact check looks at -- and leaves red
    shining into the room, so contact reads fine and the ratio is noise."""
    monitor = build(spo2_module, FakeSensor())

    quality = monitor.assess_signal(window(dc=900, ac=50), window())

    assert quality == spo2_module.SpO2Monitor.QUALITY_PARTIAL_CONTACT
    # the dark channel is kept for the log; last_ir_dc stays owned by the
    # contact check, which is the only place that reads fresh samples
    assert monitor.last_red_dc < 80000 * spo2_module.SpO2Monitor.MIN_RED_TO_IR_RATIO


def test_a_flat_signal_is_reported_as_no_pulse(spo2_module):
    """Squeezed capillaries (and a cold hand) leave the DC level healthy and
    the pulsatile component gone -- the 'contact is fine, still no reading'
    case that used to print the same 'weak signal' as everything else."""
    monitor = build(spo2_module, FakeSensor())

    quality = monitor.assess_signal(window(dc=40000, ac=0), window(dc=80000, ac=0))

    assert quality == spo2_module.SpO2Monitor.QUALITY_NO_PULSE
    assert monitor.last_perfusion == 0.0


def test_a_single_spike_does_not_pass_as_a_pulse(spo2_module):
    """min/max would read one knock as a healthy amplitude, so the amplitude
    is measured between the 2nd and 98th percentile instead."""
    monitor = build(spo2_module, FakeSensor())
    ir = window(dc=80000, ac=0)
    ir[50] = 120000

    assert monitor.assess_signal(window(dc=40000, ac=0), ir) == \
        spo2_module.SpO2Monitor.QUALITY_NO_PULSE


def test_an_empty_window_is_not_classified(spo2_module):
    monitor = build(spo2_module, FakeSensor())
    assert monitor.assess_signal([], []) is None


def test_the_dominant_cause_wins_not_the_last_one(spo2_module):
    """The window that happens to land last is no more representative than any
    other, so the message is decided by what was seen most."""
    monitor = build(spo2_module, FakeSensor())
    saturated = (window(dc=130000, ac=4000), window(dc=260000, ac=8000))
    for _ in range(3):
        monitor.assess_signal(*saturated)
    monitor.assess_signal(window(dc=900, ac=50), window())

    assert monitor.last_quality == spo2_module.SpO2Monitor.QUALITY_PARTIAL_CONTACT
    assert monitor.dominant_quality == spo2_module.SpO2Monitor.QUALITY_SATURATED


def test_uncomputable_windows_are_classified_during_a_run(spo2_module, monkeypatch):
    """End to end: contact is good, the algorithm never computes, and the run
    ends knowing it was the flat signal -- not just that it failed."""
    sensor = FakeSensor()  # flat blocks: DC fine, no pulse at all
    script_calc(monkeypatch, spo2_module, [None])
    monitor = build(spo2_module, sensor, max_wait_seconds=0.15)

    assert monitor.measure_spo2(on_progress=quiet) is None
    assert monitor.last_error == spo2_module.SpO2Monitor.ERR_WEAK
    assert monitor.dominant_quality == spo2_module.SpO2Monitor.QUALITY_NO_PULSE


def test_a_fifo_gap_is_recorded_as_its_own_cause(spo2_module, monkeypatch):
    """A discarded window is not a weak signal -- the two used to print the
    same line, which hid dropped samples behind 'place your finger better'."""
    sensor = OverflowingSensor(overflow_after=(2,))
    script_calc(monkeypatch, spo2_module, [97])
    monitor = build(spo2_module, sensor)

    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert monitor.quality_counts[spo2_module.SpO2Monitor.QUALITY_FIFO_GAP] == 1


def test_a_new_run_starts_from_a_clean_verdict(spo2_module, monkeypatch):
    sensor = FakeSensor()
    script_calc(monkeypatch, spo2_module, [None])
    monitor = build(spo2_module, sensor, max_wait_seconds=0.15)
    monitor.measure_spo2(on_progress=quiet)

    script_calc(monkeypatch, spo2_module, [97])
    assert monitor.measure_spo2(on_progress=quiet) == 97
    assert monitor.quality_counts == {}


def test_default_progress_names_the_cause(spo2_module, capsys):
    """The printed line is the field-debugging path on the Pi, so it has to
    carry the verdict; an unclassified window keeps the old wording."""
    progress = spo2_module.SpO2Monitor.default_progress

    progress(None, bpm=0, stable=False, finger_detected=True)
    progress(None, bpm=0, stable=False, finger_detected=True,
             quality=spo2_module.SpO2Monitor.QUALITY_SATURATED)
    progress(None, bpm=0, stable=False, finger_detected=True,
             quality=spo2_module.SpO2Monitor.QUALITY_NO_PULSE)
    lines = capsys.readouterr().out.splitlines()

    assert "weak signal" in lines[0]
    assert "press lighter" in lines[1]
    assert "cold hand" in lines[2]


# ── a sensor that stops sending ───────────────────────────────────────────

class StalledSensor(FakeSensor):
    """A sensor whose reads time out: the real driver returns a short buffer
    and sets last_read_timed_out rather than spinning for ever."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_read_timed_out = False

    def read_sequential(self, amount=110):
        self.requested.append(amount)
        self.last_read_timed_out = True
        return [], []


def test_a_sensor_that_sends_nothing_is_not_blamed_on_the_finger(spo2_module):
    """Contact is judged on samples, so no samples reads as "no finger" --
    true, and useless: it sends the operator to reposition a finger when the
    I2C wiring is what needs looking at."""
    monitor = build(spo2_module, StalledSensor(), max_wait_seconds=0.15)

    assert monitor.measure_spo2(on_progress=quiet) is None
    assert monitor.last_error == spo2_module.SpO2Monitor.ERR_NO_DATA
    assert monitor.stalled_reads > 0


def test_a_stall_after_contact_still_reports_the_signal_problem(spo2_module, monkeypatch):
    """Once a finger has been seen, the run is about signal quality; a late
    stalled read must not relabel it as a wiring fault."""

    class LateStall(FakeSensor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.last_read_timed_out = False

        def read_sequential(self, amount=110):
            if len(self.requested) >= 2:
                self.last_read_timed_out = True
            return super().read_sequential(amount)

    script_calc(monkeypatch, spo2_module, [None])
    monitor = build(spo2_module, LateStall(), max_wait_seconds=0.15)

    assert monitor.measure_spo2(on_progress=quiet) is None
    assert monitor.last_error == spo2_module.SpO2Monitor.ERR_WEAK
