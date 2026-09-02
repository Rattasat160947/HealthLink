# -*- coding: utf-8 -*-
"""Settling behaviour of the DS18B20 body-temperature probe.

lib/temp_sensor.py reads /sys/bus/w1, which does not exist off the Pi, so the
sensor is built with an explicit id (that skips the device glob) and its single
raw read is scripted. What is under test is the loop on top of that read -- the
part that decides whether a measurement produces a value at all.
"""
from __future__ import annotations

import pytest

from lib.temp_sensor import temp_sensor


def build(monkeypatch, temps, **kwargs):
    """A probe that returns `temps` in order (the last value repeats).

    An element may be an exception instance, which is raised instead -- that is
    what a corrupt 1-Wire frame or an unplugged probe looks like from here."""
    kwargs.setdefault("max_wait_seconds", 5)
    kwargs.setdefault("poll_interval", 0)
    sensor = temp_sensor(sensor_id="28-test", **kwargs)

    calls = {"n": 0}

    def scripted():
        value = temps[min(calls["n"], len(temps) - 1)]
        calls["n"] += 1
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(sensor, "read_celsius_once", scripted)
    sensor.calls = calls
    return sensor


def quiet(temp, stable, in_contact):
    """on_progress=None means "print", so tests that do not inspect progress
    pass this instead of leaving the loop printing."""


# ── the threshold has to be something the hardware can express ────────────

def test_stability_threshold_is_above_the_sensor_resolution():
    """The regression this file exists for.

    A DS18B20 at 12-bit quantises to 0.0625 C, so `max - min <= 0.05` could
    only ever be satisfied by five BIT-IDENTICAL readings -- not by five steady
    ones. A probe still warming towards the skin rarely manages that, which is
    why the same kiosk settled on one attempt and timed out on the next."""
    sensor = temp_sensor(sensor_id="28-test")
    assert sensor.stability_threshold >= temp_sensor.SENSOR_RESOLUTION


def test_a_window_holding_within_one_step_settles(monkeypatch):
    """Readings one quantisation step apart are as steady as this sensor can
    report. Under the old 0.05 threshold this exact run never settled."""
    step = temp_sensor.SENSOR_RESOLUTION
    sensor = build(monkeypatch, [36.5, 36.5 + step, 36.5, 36.5 + step, 36.5])

    assert sensor.measure_body_temperature(on_progress=quiet) is not None


def test_a_single_outlier_does_not_reset_a_settled_window(monkeypatch):
    """One conversion landing a few steps off is noise on the bus, not the
    patient's temperature moving; at ~750 ms per conversion, restarting the
    window over it costs a real share of the timeout."""
    sensor = build(monkeypatch, [36.5, 36.5, 36.8, 36.5, 36.5])

    assert sensor.measure_body_temperature(on_progress=quiet) == 36.5
    assert sensor.calls["n"] == 5


def test_trimming_does_not_forgive_a_still_drifting_probe():
    """Tolerating one outlier must not turn a probe still warming towards the
    skin into a result: there, every reading moves, and dropping the most
    deviant one leaves a spread that is still too wide."""
    trim = temp_sensor._trimmed_spread

    assert trim([36.5, 36.5, 36.8, 36.5, 36.5]) <= 0.2   # one outlier
    assert trim([36.0, 36.2, 36.4, 36.6, 36.8]) > 0.2    # still climbing


def test_the_median_of_the_window_is_returned(monkeypatch):
    sensor = build(monkeypatch, [36.4, 36.5, 36.6, 36.5, 36.5])

    assert sensor.measure_body_temperature(on_progress=quiet) == 36.5


# ── skin contact ──────────────────────────────────────────────────────────

def test_a_typical_skin_reading_counts_as_contact():
    """This is a contact probe, not a core thermometer: held against skin it
    reads a few degrees under core. A 34.0 floor sat on top of that band, so
    ordinary readings registered as "probe not touching" and cleared the
    window."""
    sensor = temp_sensor(sensor_id="28-test")

    assert sensor.is_probe_in_contact(33.0)
    assert not sensor.is_probe_in_contact(25.0)   # room temperature
    assert not sensor.is_probe_in_contact(45.0)


def test_losing_contact_still_clears_the_window(monkeypatch):
    """Deliberately NOT softened: an out-of-range reading means the probe came
    off the skin, and a window mixing on-skin and off-skin samples would report
    a body temperature nobody had."""
    sensor = build(monkeypatch, [36.5, 36.5, 20.0, 36.5, 36.5, 36.5, 36.5, 36.5])

    assert sensor.measure_body_temperature(on_progress=quiet) == 36.5
    # Eight reads: the two taken before the probe came off are NOT carried
    # into the window that produced the result -- five fresh ones are taken
    # after it. That is the whole point of clearing here.
    assert sensor.calls["n"] == 8


# ── read failures ─────────────────────────────────────────────────────────

def test_one_corrupt_frame_is_skipped_not_fatal(monkeypatch):
    """A CRC failure is one bad 1-Wire frame; the bus recovers by itself. It
    used to abort the whole measurement and reach the operator as "วัดอุณหภูมิ
    ไม่สำเร็จ", which told them to do something about a glitch they had no part
    in."""
    sensor = build(
        monkeypatch,
        [36.5, Exception("CRC Error"), 36.5, 36.5, 36.5, 36.5],
    )

    assert sensor.measure_body_temperature(on_progress=quiet) == 36.5
    assert sensor.read_errors == 1


def test_a_dead_probe_still_raises(monkeypatch):
    """Skipping bad frames must not hide a probe that is simply gone: several
    failures in a row is a wiring fault and has to surface as one, not as
    "hold still" for the rest of the timeout."""
    sensor = build(monkeypatch, [FileNotFoundError("w1_slave")])

    with pytest.raises(FileNotFoundError):
        sensor.measure_body_temperature(on_progress=quiet)
    assert sensor.read_errors == temp_sensor.MAX_CONSECUTIVE_READ_ERRORS


def test_scattered_bad_frames_do_not_add_up_to_a_dead_probe(monkeypatch):
    """The give-up count is consecutive failures, not total ones: a noisy
    cable that still reads fine in between is a measurement that should
    finish."""
    sensor = build(
        monkeypatch,
        [Exception("CRC Error"), 36.5, Exception("CRC Error"), 36.5,
         Exception("CRC Error"), 36.5, 36.5, 36.5, 36.5],
    )

    assert sensor.measure_body_temperature(on_progress=quiet) == 36.5
    assert sensor.read_errors == 3
