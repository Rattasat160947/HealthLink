# -*- coding: utf-8 -*-
"""MAX30102 pulse-oximeter helper.

`GetSpO2Sensor()` returns ONE instantaneous estimate: the algorithm recomputes
HR/SpO2 from scratch on every call, so consecutive calls jitter by a few points
and there is never a "final" value -- the caller just grabs whatever window it
happened to land on.

`measure_spo2()` adds the missing settling step, mirroring
lib/temp_sensor.py's measure_body_temperature(): keep reading until N
consecutive windows agree within a threshold, then return the median of that
window as the result.
"""
try:
    from .max30102 import MAX30102
    from .hrcalc import calc_hr_and_spo2, BUFFER_SIZE
except ImportError:
    from max30102 import MAX30102
    from hrcalc import calc_hr_and_spo2, BUFFER_SIZE

import os
import statistics
import sys
import time
import numpy as np


class SpO2Monitor():
    # calc_hr_and_spo2() indexes a fixed 100-sample window (hrcalc.BUFFER_SIZE),
    # which is 4 s of signal at the 25 Hz effective output rate configured in
    # max30102.py (100 Hz sampling, 4-sample averaging).
    WINDOW_SAMPLES = BUFFER_SIZE
    # Fresh samples pulled between two estimates: 25 = 1 s. The window slides
    # instead of being refilled, so a new value appears every second while the
    # algorithm still sees 4 s of signal (this is how Maxim's reference loop
    # runs). stability_window * STEP_SAMPLES >= WINDOW_SAMPLES, so the first and
    # last reading of a "stable" window share no samples -- they are genuinely
    # independent measurements, not the same 4 s recomputed 5 times.
    STEP_SAMPLES = 25

    # physiologically plausible windows; anything outside is a bad computation
    MIN_VALID_SPO2 = 70
    MAX_VALID_SPO2 = 100
    MIN_VALID_BPM = 30
    MAX_VALID_BPM = 220

    # Mean IR count below this means no finger on the sensor (ambient light
    # only). Tune per unit if the LED currents in max30102.py are changed:
    # `python -m lib.spo2_max30102.spo2_monitor --raw` prints the live IR DC
    # level, so put a finger on and off and pick a value between the two.
    # 50000 was too high for these breakout boards at the old 7 mA drive --
    # a real finger sat under it, so the loop kept restarting on "no finger"
    # until it ran out of time. Override per unit with
    # CAREKEEPER_SPO2_FINGER_IR_THRESHOLD.
    FINGER_IR_THRESHOLD = int(os.environ.get(
        "CAREKEEPER_SPO2_FINGER_IR_THRESHOLD", "10000"
    ))

    # Why a measurement came back empty, for the caller's error message.
    ERR_NO_FINGER = "NO_FINGER"    # never saw contact for long enough
    ERR_UNSTABLE  = "UNSTABLE"     # had contact, readings never agreed
    ERR_WEAK      = "WEAK_SIGNAL"  # had contact, algorithm never got a value

    def __init__(self, sensor=None, stability_window=5, stability_threshold=3.0,
                 max_wait_seconds=60, finger_ir_threshold=None):
        self.bpm = 0
        self.spo2 = 0
        self.m = sensor if sensor is not None else MAX30102()
        self.stability_window = stability_window
        self.stability_threshold = stability_threshold
        self.max_wait_seconds = max_wait_seconds
        self.finger_ir_threshold = (
            self.FINGER_IR_THRESHOLD if finger_ir_threshold is None else finger_ir_threshold
        )
        self.last_error = None      # one of the ERR_* codes after a failure
        self.last_ir_dc = 0         # IR DC level last seen, for calibration
        self.overflows = 0          # FIFO gaps discarded during the last run
        self._red = []
        self._ir = []

    def GetSpO2Sensor(self):
        """One instantaneous reading: (bpm, spo2, red, ir).

        Kept unchanged for raw/debug use. The values jitter between calls --
        use measure_spo2() when a final value is needed."""
        hr2 = 0
        sp2 = 0

        red, ir = self.m.read_sequential()
        hr, hrb, sp, spb = calc_hr_and_spo2(np.array(ir), np.array(red))

        if hrb == True and hr != -999:
            hr2 = int(hr)
        if spb == True and sp != -999:
            sp2 = int(sp)

        return hr2, sp2, red, ir

    def is_finger_present(self, ir):
        """A finger blocks ambient light and reflects the IR LED, so the DC
        level of the IR channel is the cheapest contact check available --
        the equivalent of the temperature probe's skin-contact range check."""
        if len(ir) == 0:
            self.last_ir_dc = 0
            return False
        self.last_ir_dc = int(np.mean(ir))
        return self.last_ir_dc >= self.finger_ir_threshold

    def _fifo_overflowed(self):
        """True if the sensor dropped samples since the last check, meaning the
        window now splices two stretches of signal with a gap between them.

        Guarded by getattr because injected/fake sensors need not model the
        FIFO's overflow counter."""
        count = getattr(self.m, "get_overflow_count", None)
        if count is None:
            return False
        try:
            lost = count()
        except Exception:
            return False
        if not lost:
            return False
        self.overflows += 1
        clear = getattr(self.m, "clear_overflow_count", None)
        if clear is not None:
            try:
                clear()
            except Exception:
                pass
        return True

    def _slide_window(self):
        """Refresh the rolling sample window: returns (red, ir, fresh_ir).

        The first call fills the whole 100-sample window (~4 s); later calls
        pull only STEP_SAMPLES (~1 s) and drop the oldest samples. The freshly
        read IR samples are returned separately because that is what the
        contact check must look at (see measure_spo2).

        read_sequential() can return slightly more samples than asked (it
        drains whatever the FIFO holds), so the window is trimmed by slicing
        rather than assuming an exact length."""
        amount = (
            self.WINDOW_SAMPLES
            if len(self._ir) < self.WINDOW_SAMPLES
            else self.STEP_SAMPLES
        )
        red, ir = self.m.read_sequential(amount)
        self._red = (self._red + list(red))[-self.WINDOW_SAMPLES:]
        self._ir = (self._ir + list(ir))[-self.WINDOW_SAMPLES:]
        return self._red, self._ir, list(ir)

    def _clear_window(self):
        self._red = []
        self._ir = []

    @staticmethod
    def default_progress(spo2, bpm=0, stable=False, finger_detected=True):
        if not finger_detected:
            print("  --  --> Fail - Place a finger on the sensor and keep it still !!!")
        elif spo2 is None:
            print("  --  (weak signal, measuring...)")
        else:
            status = "stable" if stable else "measuring..."
            print(f"  SpO2 {spo2}%  BPM {bpm}  ({status})")

    def measure_spo2(self, on_progress=None):
        """Block until the reading settles, then return the final SpO2 (int),
        or None if it never settles within max_wait_seconds.

        Same contract as temp_sensor.measure_body_temperature(): readings go
        into a rolling window of `stability_window` values, the measurement is
        done once the spread of that window is within `stability_threshold`
        percentage points, and the MEDIAN is returned -- a median so one
        artifact window cannot drag the result the way a mean would.

        On success the matching pulse rate is left on self.bpm (and the result
        on self.spo2) for callers that want it; SpO2 alone is the return value
        so the call site reads like the temperature one."""
        if on_progress is None:
            on_progress = self.default_progress

        readings = []
        pulses = []
        self._clear_window()
        self.last_error = self.ERR_NO_FINGER
        self.overflows = 0
        start = time.time()

        while time.time() - start < self.max_wait_seconds:
            red, ir, fresh_ir = self._slide_window()

            # Contact is judged on the FRESHLY read samples, not on the whole
            # window: right after the finger comes off, 75% of the window is
            # still "finger on" data, so the window average would keep saying
            # "in contact" for about three more seconds -- long enough for
            # windows straddling the lift to be measured as if they were real.
            if not self.is_finger_present(fresh_ir):
                # Drop the progress made so far and start the window over --
                # the same thing the temperature probe does when it loses skin
                # contact. Because the window is refilled from scratch, no
                # window can ever mix samples from before and after a lift.
                readings.clear()
                pulses.clear()
                self._clear_window()
                if on_progress:
                    on_progress(None, bpm=0, stable=False, finger_detected=False)
                continue

            # Contact was made, so "no finger" is no longer the story; from
            # here the failure is about signal quality, not placement.
            if self.last_error == self.ERR_NO_FINGER:
                self.last_error = self.ERR_WEAK

            if self._fifo_overflowed():
                # The sensor dropped samples while we were busy, so this window
                # splices two stretches of signal across a gap. Peak intervals
                # and the AC/DC ratio computed across that seam are wrong in a
                # way nothing downstream can see, so start the window over
                # rather than feed it to the algorithm.
                readings.clear()
                pulses.clear()
                self._clear_window()
                if on_progress:
                    on_progress(None, bpm=0, stable=False, finger_detected=True)
                continue

            if len(ir) < self.WINDOW_SAMPLES:
                continue  # not enough signal buffered yet for the algorithm

            hr, hr_valid, sp, sp_valid = calc_hr_and_spo2(np.array(ir), np.array(red))
            bpm = int(hr) if hr_valid and self.MIN_VALID_BPM <= hr <= self.MAX_VALID_BPM else 0

            if not sp_valid or not (self.MIN_VALID_SPO2 <= sp <= self.MAX_VALID_SPO2):
                # One unusable window (motion artifact / weak perfusion): skip
                # it but keep what we already have. If the value really moved,
                # the spread check below rejects the window anyway.
                if on_progress:
                    on_progress(None, bpm=bpm, stable=False, finger_detected=True)
                continue

            # The algorithm produced a usable value, so any failure from here
            # is the readings refusing to agree, not a signal we never got.
            self.last_error = self.ERR_UNSTABLE

            readings.append(float(sp))
            pulses.append(bpm)
            if len(readings) > self.stability_window:
                readings.pop(0)
                pulses.pop(0)

            stable = (
                len(readings) == self.stability_window
                and (max(readings) - min(readings)) <= self.stability_threshold
            )

            if on_progress:
                on_progress(int(round(sp)), bpm=bpm, stable=stable, finger_detected=True)

            if stable:
                valid_pulses = [p for p in pulses if p]
                self.spo2 = int(round(statistics.median(readings)))
                self.bpm = int(statistics.median(valid_pulses)) if valid_pulses else 0
                self.last_error = None
                return self.spo2

        return None


def main():
    """Take one settled measurement (what the app does)."""
    print('sensor starting...')
    sensor = SpO2Monitor()
    print("Place a finger on the sensor and hold still...")
    try:
        result = sensor.measure_spo2()
    finally:
        sensor.m.shutdown()

    if result is None:
        print("Measurement failed: reading did not stabilize within the time limit")
    else:
        print(f"SpO2: {result}%  (pulse: {sensor.bpm} bpm)")


def main_raw():
    """Continuous unsettled readings -- for checking the hardware/signal."""
    print('sensor starting...')
    SpO2_Sensor = SpO2Monitor()
    LOOP_TIME = 1

    while True:
        bpm, spo2, red, ir = SpO2_Sensor.GetSpO2Sensor()
        # The IR DC level is printed so FINGER_IR_THRESHOLD can be calibrated
        # against the actual unit: put a finger on and off, and pick a value
        # between the two levels you see.
        ir_dc = int(np.mean(ir)) if len(ir) else 0
        contact = "finger" if ir_dc >= SpO2_Sensor.finger_ir_threshold else "NO finger"
        print("BPM: {}, SpO2: {}, IR dc: {} ({})".format(bpm, spo2, ir_dc, contact))
        time.sleep(LOOP_TIME)


if __name__ == '__main__':
    if "--raw" in sys.argv:
        main_raw()
    else:
        main()
