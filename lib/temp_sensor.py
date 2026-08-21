import glob
import time
import statistics


class temp_sensor:
    # valid temperature range
    MIN_VALID_TEMP = 34.0
    MAX_VALID_TEMP = 43.0

    def __init__(self, sensor_id=None, calibration_offset=1.0,
                 stability_window=5, stability_threshold=0.05,
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

    def measure_body_temperature(self, on_progress=None):
        if on_progress is None:
            on_progress = self.default_progress

        readings = []
        start = time.time()

        while time.time() - start < self.max_wait_seconds:
            temp = self.read_celsius_once()

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
                and (max(readings) - min(readings)) <= self.stability_threshold
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