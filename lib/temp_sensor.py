import glob
import time
import statistics


class temp_sensor:
    # Valid skin-contact range. The floor is NOT body temperature: this is a
    # contact probe held against the skin, which reads a few degrees under
    # core -- roughly 33-36 C before calibration_offset is added. A floor of
    # 34.0 sat right on top of that band, so an ordinary reading landing one
    # step low counted as "probe not touching skin" and threw the whole
    # settling window away. 32.0 clears the skin band while still rejecting
    # room temperature, which is what the check is actually for.
    MIN_VALID_TEMP = 32.0
    MAX_VALID_TEMP = 43.0

    # A DS18B20 at its default 12-bit resolution quantises to 0.0625 C per
    # step, so a stability threshold BELOW that cannot mean "the reading is
    # steady" -- it can only mean "every sample came back bit-identical",
    # which a probe still warming towards the skin rarely manages. The old
    # default of 0.05 was such a threshold, and it is the reason a
    # measurement settled on some attempts and ran the clock out on others.
    SENSOR_RESOLUTION = 0.0625

    # Consecutive failed reads that mean the device itself is gone (unplugged
    # probe, w1 module not loaded) rather than one corrupt frame. Below this,
    # a failure is skipped and the measurement carries on.
    MAX_CONSECUTIVE_READ_ERRORS = 3

    def __init__(self, sensor_id=None, calibration_offset=1.0,
                 stability_window=5, stability_threshold=0.2,
                 max_wait_seconds=60, poll_interval=0.05):
        if sensor_id is None:
            devices = glob.glob("/sys/bus/w1/devices/28-*")
            if len(devices) == 0:
                raise Exception("temp sensor is not found")
            self.device = devices[0]
        else:
            self.device = "/sys/bus/w1/devices/" + sensor_id

        self.device_file = self.device + "/w1_slave"
        self.calibration_offset = calibration_offset
        self.stability_window = stability_window
        self.stability_threshold = stability_threshold
        self.max_wait_seconds = max_wait_seconds
        self.poll_interval = poll_interval
        # Corrupt 1-Wire frames skipped during the last measurement. Zero on a
        # healthy bus; a rising count is the symptom of a long or noisy cable.
        self.read_errors = 0

    def _read_raw(self):
        with open(self.device_file, "r") as f:
            return f.readlines()

    def read_celsius_once(self):
        lines = self._read_raw()
        retry = 0
        while "YES" not in lines[0]:
            time.sleep(0.2)
            lines = self._read_raw()
            retry += 1
            if retry > 5:
                raise Exception("CRC Error")

        raw_temp = float(lines[1].split("t=")[1]) / 1000
        return raw_temp + self.calibration_offset

    def is_probe_in_contact(self, temp):
        return self.MIN_VALID_TEMP <= temp <= self.MAX_VALID_TEMP

    @staticmethod
    def default_progress(temp, stable, in_contact):
        if not in_contact:
            # print(f"  Fail {temp:.2f}°C (not in contact with skin, or out of range)")
            print(f"  {temp:.2f}°C --> Fail - Sensor is not in contact with skin or temperature out of range !!!")
        else:
            status = "stable" if stable else "measuring..."
            print(f"  {temp:.2f}°C  ({status})")

    @staticmethod
    def _trimmed_spread(values):
        """Spread of the window ignoring its single most deviant reading.

        The 1-Wire conversion occasionally lands a quantisation step away from
        its neighbours for one sample. Judging stability on the raw max-min
        lets that one sample hold up a window that has otherwise settled, and
        each conversion costs ~750 ms inside the kernel -- so only about 35 of
        them fit in the timeout, and a window rejected over a single outlier
        is a real share of the whole budget. The value returned to the caller
        is still the median of the FULL window, which one outlier cannot move
        either way."""
        if len(values) < 3:
            return max(values) - min(values) if values else 0.0
        middle = statistics.median(values)
        kept = sorted(values, key=lambda v: abs(v - middle))[:-1]
        return max(kept) - min(kept)

    def measure_body_temperature(self, on_progress=None):
        if on_progress is None:
            on_progress = self.default_progress

        readings = []
        self.read_errors = 0
        consecutive_errors = 0
        start = time.time()

        while time.time() - start < self.max_wait_seconds:
            try:
                temp = self.read_celsius_once()
            except Exception:
                # One corrupt 1-Wire frame is not a failed measurement: the
                # bus recovers by itself and the next conversion is usually
                # clean. Aborting the whole run on it turned a momentary
                # glitch into "วัดอุณหภูมิไม่สำเร็จ" for the operator, so the
                # sample is skipped and the clock keeps running instead. The
                # window is deliberately NOT cleared -- a bad frame says
                # nothing about whether the probe is still on the skin.
                #
                # Several in a row is a different story (probe unplugged, w1
                # module gone), and that must surface as the hardware error it
                # is rather than as "hold still" for the rest of the timeout.
                self.read_errors += 1
                consecutive_errors += 1
                if consecutive_errors >= self.MAX_CONSECUTIVE_READ_ERRORS:
                    raise
                time.sleep(self.poll_interval)
                continue
            consecutive_errors = 0

            if not self.is_probe_in_contact(temp):
                readings.clear()
                if on_progress:
                    on_progress(temp, stable=False, in_contact=False)
                time.sleep(self.poll_interval)
                continue

            readings.append(temp)
            if len(readings) > self.stability_window:
                readings.pop(0)

            stable = (
                len(readings) == self.stability_window
                and self._trimmed_spread(readings) <= self.stability_threshold
            )

            if on_progress:
                on_progress(temp, stable=stable, in_contact=True)

            if stable:
                return round(statistics.median(readings), 2)

            time.sleep(self.poll_interval)

        return None

    def read_fahrenheit(self, celsius_value):
        return celsius_value * 9 / 5 + 32

    def get_id(self):
        return self.device.split("/")[-1]

    @staticmethod
    def list_sensors():
        sensors = []
        for dev in glob.glob("/sys/bus/w1/devices/28-*"):
            sensors.append(dev.split("/")[-1])
        return sensors


if __name__ == "__main__":
    sensor = temp_sensor()

    print("Place the sensor against the skin and wait for the result...")
    result = sensor.measure_body_temperature()

    if result is None:
        print("Measurement failed: reading did not stabilize within the time limit")
    else:
        print(f"Body temperature: {result:.2f}°C")