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
    ERR_NO_DATA   = "NO_DATA"      # the part never produced a single sample

    # ── why a window was uncomputable ────────────────────────────────────
    # "Finger on the sensor but no value" has several causes, and the fix for
    # each is a different -- sometimes opposite -- instruction to the person
    # being measured: press LIGHTER, or cover MORE of the sensor, or warm the
    # hand. One catch-all "weak signal" asked them to guess, so every failed
    # window is classified against the raw samples instead.
    QUALITY_SATURATED       = "SATURATED"        # ADC clipped: peaks cut flat
    QUALITY_PARTIAL_CONTACT = "PARTIAL_CONTACT"  # one LED path uncovered
    QUALITY_NO_PULSE        = "NO_PULSE"         # DC fine, no pulsatile AC
    QUALITY_FIFO_GAP        = "FIFO_GAP"         # samples dropped mid-window

    # Field-debugging text for each, printed by default_progress(). The Thai
    # wording the kiosk shows lives in carekeeper_providers.py, like every
    # other operator-facing message.
    QUALITY_HINTS = {
        QUALITY_SATURATED:       "signal saturated -- press lighter / lower LED current",
        QUALITY_PARTIAL_CONTACT: "finger off-centre -- cover both LEDs and the detector",
        QUALITY_NO_PULSE:        "no pulse in the signal -- pressing too hard, or cold hand",
        QUALITY_FIFO_GAP:        "samples dropped, window discarded",
    }

    # 18-bit samples (LED_PW = 411 us in max30102.py's SPO2_CONFIG), so a
    # reading tops out at 0x3FFFF. With DEFAULT_LED_CURRENT at its 12.6 mA
    # maximum a firmly pressed finger can park the ADC there: the DC level
    # then looks excellent while the crests of the pulse waveform are cut
    # flat, leaving find_peaks() nothing to find.
    ADC_FULL_SCALE = 0x3FFFF
    SATURATION_LEVEL = int(ADC_FULL_SCALE * 0.98)
    # A lone clipped sample is noise; 1% of the window is clipping.
    SATURATION_SAMPLE_FRACTION = 0.01

    # Red and IR are two LEDs side by side under one detector, so a finger
    # covering the window lights both: red DC sits lower than IR (haemoglobin
    # absorbs red) but stays the same order of magnitude. A finger resting on
    # one edge covers one LED and leaves the other shining into the room --
    # that reads as solid contact on IR while the red/IR ratio the SpO2 maths
    # is built on comes out of noise.
    MIN_RED_TO_IR_RATIO = 0.10

    # Perfusion index: pulsatile amplitude as a percentage of the DC level.
    # A finger resting on the sensor gives roughly 0.5-5%. Pressing hard
    # enough to squeeze the capillaries empty -- and a cold hand -- drive it
    # towards zero while the DC level stays perfectly healthy, which is
    # exactly the "contact is fine, still no reading" case.
    MIN_PERFUSION_INDEX = 0.15

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
        self.reads = 0              # window reads that returned samples
        self.stalls = 0             # window reads that came back empty
        self.last_quality = None    # QUALITY_* code of the last failed window
        self.quality_counts = {}    # how often each QUALITY_* code was seen
        self.last_red_dc = 0        # red DC level, for the coverage check
        self.last_ir_ac = 0.0       # pulsatile amplitude of the last window
        self.last_perfusion = 0.0   # that amplitude as a % of the DC level
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

    def _record_quality(self, quality):
        """Remember a window's verdict. The counts, not just the last one,
        decide the final message: the window that happened to land last is no
        more representative than any other, and a run that spent 25 s clipped
        and one window off-centre should say "press lighter"."""
        self.last_quality = quality
        if quality is not None:
            self.quality_counts[quality] = self.quality_counts.get(quality, 0) + 1
        return quality

    @property
    def dominant_quality(self):
        """The QUALITY_* code seen most often during the last run, or None."""
        if not self.quality_counts:
            return None
        return max(self.quality_counts, key=self.quality_counts.get)

    def assess_signal(self, red, ir):
        """Say WHY calc_hr_and_spo2() could not compute this window.

        Returns a QUALITY_* code, or None when the raw signal looks usable and
        the failure was the algorithm's own (a beat lost to movement, a ratio
        outside its calibration range) rather than something the person on the
        sensor can do anything about.

        The checks are ordered by what invalidates what: clipping makes the
        amplitude meaningless, and an uncovered LED makes both channels
        meaningless, so those are decided before the pulse is judged."""
        ir_arr = np.asarray(ir, dtype=float)
        red_arr = np.asarray(red, dtype=float)
        if ir_arr.size == 0:
            return self._record_quality(None)

        ir_dc = float(ir_arr.mean())
        red_dc = float(red_arr.mean()) if red_arr.size else 0.0
        # 2nd/98th percentile rather than min/max: a single spike from the
        # sensor being knocked would otherwise pass as a healthy pulse.
        low, high = np.percentile(ir_arr, (2, 98))
        self.last_red_dc = int(red_dc)
        self.last_ir_ac = float(high - low)
        self.last_perfusion = (100.0 * self.last_ir_ac / ir_dc) if ir_dc else 0.0

        clipped = int(
            np.count_nonzero(ir_arr >= self.SATURATION_LEVEL)
            + np.count_nonzero(red_arr >= self.SATURATION_LEVEL)
        )
        clip_limit = max(1, int(self.SATURATION_SAMPLE_FRACTION * ir_arr.size))

        if clipped >= clip_limit:
            return self._record_quality(self.QUALITY_SATURATED)
        if red_dc < ir_dc * self.MIN_RED_TO_IR_RATIO:
            return self._record_quality(self.QUALITY_PARTIAL_CONTACT)
        if self.last_perfusion < self.MIN_PERFUSION_INDEX:
            return self._record_quality(self.QUALITY_NO_PULSE)
        return self._record_quality(None)

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

    def _reset_fifo(self):
        """Drop the samples the sensor buffered while we were not reading.

        Clearing the window in software is only half of it: the MAX30102 has
        been sampling into its own 32-deep FIFO the whole time, which is just
        1.28 s at 25 Hz with rollover off. Everything between opening the
        sensor and the first window -- and everything spent computing a window
        that was then discarded -- has already filled that buffer and left the
        overflow counter set.

        Two things followed from not doing this. The first window of every run
        was built partly from samples taken before the finger was even on the
        sensor, and _fifo_overflowed() then discarded it on the strength of an
        overflow that happened while the kiosk was idle -- so ~4 s of a 30 s
        budget went every single run, before the person being measured had
        done anything wrong.

        getattr-guarded because injected/fake sensors need not model a FIFO."""
        reset = getattr(self.m, "flush_fifo", None)
        if reset is None:
            # Older driver: at least clear the counter so a stale overflow
            # does not condemn the window we are about to fill.
            reset = getattr(self.m, "clear_overflow_count", None)
        if reset is None:
            return
        try:
            reset()
        except Exception:
            pass

    @staticmethod
    def _trimmed_spread(values):
        """Spread of the window ignoring its single most deviant reading.

        The algorithm recomputes SpO2 from scratch per window, and one window
        in a run routinely lands a few points off while still inside the
        70-100 range that would have rejected it outright -- a swallow of
        breath, a knuckle shifting. On the raw max-min that one window blocks
        stability for `stability_window` more seconds, and every second spent
        is one the finger has to stay still for. What the caller receives is
        the median of the FULL window either way, and one outlier cannot move
        a median."""
        if len(values) < 3:
            return max(values) - min(values) if values else 0.0
        middle = statistics.median(values)
        kept = sorted(values, key=lambda v: abs(v - middle))[:-1]
        return max(kept) - min(kept)

    def _clear_window(self):
        self._red = []
        self._ir = []
        self._reset_fifo()

    @staticmethod
    def default_progress(spo2, bpm=0, stable=False, finger_detected=True,
                         quality=None):
        if not finger_detected:
            print("  --  --> Fail - Place a finger on the sensor and keep it still !!!")
        elif spo2 is None:
            # `quality` names the specific problem when the window could be
            # classified; "weak signal" stays the wording for the windows that
            # look fine and simply did not compute.
            hint = SpO2Monitor.QUALITY_HINTS.get(quality, "weak signal")
            print(f"  --  ({hint}, measuring...)")
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
        self.reads = 0
        self.stalls = 0
        self.last_quality = None
        self.quality_counts = {}
        start = time.time()

        while time.time() - start < self.max_wait_seconds:
            red, ir, fresh_ir = self._slide_window()

            if not fresh_ir:
                # Not one sample came back. That is the part failing to
                # produce data -- an I2C fault, a stalled FIFO, a sensor that
                # was shut down -- and it is NOT a finger problem, however
                # much it looks like one from the DC level (which is zero
                # because there is nothing to average). Telling somebody to
                # place their finger is unactionable advice for a wiring
                # fault, so the two are counted apart and named apart below.
                #
                # Reachable at all only because read_sequential() gives up
                # once the FIFO stops advancing; it used to block here
                # forever, which is what "it just does not read" looked like.
                self.stalls += 1
                readings.clear()
                pulses.clear()
                self._clear_window()
                if on_progress:
                    on_progress(None, bpm=0, stable=False, finger_detected=False)
                continue

            self.reads += 1

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
                self._record_quality(self.QUALITY_FIFO_GAP)
                if on_progress:
                    on_progress(None, bpm=0, stable=False, finger_detected=True,
                                quality=self.QUALITY_FIFO_GAP)
                continue

            if len(ir) < self.WINDOW_SAMPLES:
                continue  # not enough signal buffered yet for the algorithm

            hr, hr_valid, sp, sp_valid = calc_hr_and_spo2(np.array(ir), np.array(red))
            bpm = int(hr) if hr_valid and self.MIN_VALID_BPM <= hr <= self.MAX_VALID_BPM else 0

            if not sp_valid or not (self.MIN_VALID_SPO2 <= sp <= self.MAX_VALID_SPO2):
                # One unusable window (motion artifact / weak perfusion): skip
                # it but keep what we already have. If the value really moved,
                # the spread check below rejects the window anyway. First ask
                # the raw samples WHY it was unusable, so a run that never
                # settles can end with an instruction instead of a shrug.
                quality = self.assess_signal(red, ir)
                if on_progress:
                    on_progress(None, bpm=bpm, stable=False, finger_detected=True,
                                quality=quality)
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
                and self._trimmed_spread(readings) <= self.stability_threshold
            )

            if on_progress:
                on_progress(int(round(sp)), bpm=bpm, stable=stable, finger_detected=True)

            if stable:
                valid_pulses = [p for p in pulses if p]
                self.spo2 = int(round(statistics.median(readings)))
                self.bpm = int(statistics.median(valid_pulses)) if valid_pulses else 0
                self.last_error = None
                return self.spo2

        if self.reads == 0 and self.stalls:
            # Not a single window in the whole run had samples in it, so
            # nothing about this failure is about where the finger was.
            self.last_error = self.ERR_NO_DATA
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
        # The same classification the settling loop uses, so the cause of a
        # run that never settles can be watched live: DC alone says nothing
        # about clipping, coverage or perfusion, which is what actually
        # decides whether the algorithm can compute anything.
        quality = SpO2_Sensor.assess_signal(red, ir)
        print("BPM: {}, SpO2: {}, IR dc: {} ({}), red dc: {}, AC: {:.0f}, "
              "perfusion: {:.2f}% -- {}".format(
                  bpm, spo2, ir_dc, contact, SpO2_Sensor.last_red_dc,
                  SpO2_Sensor.last_ir_ac, SpO2_Sensor.last_perfusion,
                  SpO2_Sensor.QUALITY_HINTS.get(quality, "signal looks usable")))
        time.sleep(LOOP_TIME)


if __name__ == '__main__':
    if "--raw" in sys.argv:
        main_raw()
    else:
        main()
