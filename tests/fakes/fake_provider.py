# -*- coding: utf-8 -*-
from __future__ import annotations

from carekeeper_providers import (
    BloodPressureReading,
    CareKeeperProvider,
    DeviceStatus,
    PatientInfo,
)


class FakeFailingProvider(CareKeeperProvider):
    """Test double with configurable per-method failure injection. Used for
    UI-level tests so they don't need real hardware/network, and don't
    require touching the always-succeeding MockCareKeeperProvider used by
    the demo UI."""

    def __init__(
        self,
        device_mac: str = "aa:bb:cc:dd:ee:ff",
        fail_times: dict[str, int] | None = None,
        send_data_result: bool = True,
        send_data_exception: Exception | None = None,
        device_status: DeviceStatus | None = None,
    ) -> None:
        self.device_mac = device_mac
        self.fail_times = dict(fail_times or {})
        self._call_counts: dict[str, int] = {}
        self.send_data_result = send_data_result
        self.send_data_exception = send_data_exception
        self.sent_payloads: list[dict] = []
        self._device_status = device_status or DeviceStatus()
        # Mirrors RealCareKeeperProvider: the BPMonitor.ERR_* code behind the
        # last empty blood-pressure reading. The GUI reads it to size the
        # NIBP cooldown, so the double has to carry it too.
        self.last_bp_error: str | None = None

    def _maybe_fail(self, name: str) -> None:
        self._call_counts[name] = self._call_counts.get(name, 0) + 1
        remaining = self.fail_times.get(name, 0)
        if self._call_counts[name] <= remaining:
            raise RuntimeError(f"fake {name} failure #{self._call_counts[name]}")

    def read_patient(self) -> PatientInfo:
        self._maybe_fail("read_patient")
        return PatientInfo(cid="1-2345-67890-12-3", th_name="Test", en_name="Test", birth_date="-", address="-")

    def measure_blood_pressure(self) -> BloodPressureReading:
        self._maybe_fail("measure_blood_pressure")
        return BloodPressureReading(systolic=120, diastolic=80, pulse=70)

    def measure_spo2(self) -> int:
        self._maybe_fail("measure_spo2")
        return 98

    def measure_temperature(self) -> float:
        self._maybe_fail("measure_temperature")
        return 36.5

    def get_device_status(self) -> DeviceStatus:
        return self._device_status

    def get_measurement_history(self, patient_id: str):
        return []

    def send_data(self, payload: dict) -> bool:
        self.sent_payloads.append(payload)
        if self.send_data_exception is not None:
            raise self.send_data_exception
        return self.send_data_result

    def scan_wifi_networks(self):
        return []

    def connect_wifi(self, ssid: str, password: str | None = None) -> bool:
        return True

    def scan_bluetooth_devices(self):
        return []

    def connect_bluetooth(self, address: str) -> bool:
        return True

    def reboot_device(self) -> bool:
        return True

    def shutdown_device(self) -> bool:
        return True
