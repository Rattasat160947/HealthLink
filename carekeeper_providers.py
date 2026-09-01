# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import time
import uuid
import requests
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from carekeeper_retry import retry_with_notify, SubsystemRegistry


PROJECT_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is listed in requirement.txt.
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(PROJECT_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()

# Keep hardware/backend values in .env instead of editing this file.
TEST_BP_PORT = _env("CAREKEEPER_BP_PORT")
# POST: บันทึกผลวัด
TEST_API_URL = _env("CAREKEEPER_API_URL")
TEST_API_KEY_HEADER = _env("CAREKEEPER_API_KEY_HEADER")
TEST_API_KEY = _env("CAREKEEPER_API_KEY")

# GET: ดึงประวัติผลวัด 4 รายการล่าสุดของ patient_id/cid นั้น
TEST_HISTORY_API_URL = _env("CAREKEEPER_HISTORY_API_URL")
TEST_HISTORY_PATIENT_ID_PARAM = _env("CAREKEEPER_HISTORY_PATIENT_ID_PARAM")
TEST_HISTORY_MAC_PARAM = _env("CAREKEEPER_HISTORY_MAC_PARAM")


def _subsystem_disabled(name: str) -> bool:
    return SubsystemRegistry.get(name).disabled



def read_device_mac() -> str:
    for address_file in sorted(Path("/sys/class/net").glob("*/address")):
        if address_file.parent.name == "lo":
            continue
        try:
            mac = address_file.read_text(encoding="utf-8").strip().lower()
            if re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac) and mac != "00:00:00:00:00:00":
                return mac
        except Exception:
            continue

    try:
        output = subprocess.check_output(["ip", "link"], text=True, errors="ignore", timeout=3)
        match = re.search(r"link/ether\s+([0-9a-f:]{17})", output, re.IGNORECASE)
        if match:
            return match.group(1).lower()
    except Exception:
        pass

    node = uuid.getnode()
    if node and (node >> 40) % 2 == 0:
        return ":".join(f"{(node >> shift) & 0xff:02x}" for shift in range(40, -1, -8))

    return "unknown"


@dataclass
class PatientInfo:
    cid: str = "-"
    th_name: str = "-"
    en_name: str = "-"
    birth_date: str = "-"
    address: str = "-"


@dataclass
class BloodPressureReading:
    systolic: int
    diastolic: int
    pulse: int


@dataclass
class DeviceStatus:
    battery_percent: int | None = 100
    battery_charging: bool = False
    wifi_connected: bool = False
    wifi_disabled: bool = False
    bp_disabled: bool = False
    spo2_disabled: bool = False
    idcard_disabled: bool = False


@dataclass
class MeasurementHistoryRecord:
    measured_at: str
    systolic: int | None
    diastolic: int | None
    pulse: int | None
    spo2: int | None
    temperature: float | None


class CareKeeperProvider:
    """Interface for all data sources used by the GUI."""

    on_retry_attempt: Callable[[str, int, int], None] | None = None
    on_retry_giveup: Callable[[str, str], None] | None = None
    on_measurement_progress: Callable[[str, object, dict], None] | None = None

    def _notify_measurement_progress(self, kind: str, value: object, **state) -> None:
        callback = getattr(self, "on_measurement_progress", None)
        if callback:
            callback(kind, value, state)

    def read_patient(self) -> PatientInfo:
        raise NotImplementedError

    def measure_blood_pressure(self) -> BloodPressureReading:
        raise NotImplementedError

    def measure_spo2(self) -> int:
        raise NotImplementedError

    def measure_temperature(self) -> float:
        raise NotImplementedError

    def get_device_status(self) -> DeviceStatus:
        raise NotImplementedError

    def get_measurement_history(self, patient_id: str) -> list[MeasurementHistoryRecord]:
        return []
    
    def send_data(self, payload: dict) -> bool:
        raise NotImplementedError

    def scan_wifi_networks(self) -> list[str]:
        return []

    def connect_wifi(self, ssid: str, password: str | None = None) -> bool:
        raise NotImplementedError

    def reboot_device(self) -> bool:
        raise NotImplementedError

    def shutdown_device(self) -> bool:
        raise NotImplementedError

    def get_ip_address(self) -> str:
        return ""


class MockCareKeeperProvider(CareKeeperProvider):
    """Development provider for UI preview without real hardware."""

    def __init__(self) -> None:
        self._battery_percent = 100
        self.device_mac = read_device_mac()

    def read_patient(self) -> PatientInfo:
        time.sleep(1.0)
        return PatientInfo(
            cid="1-2345-67890-12-3",
            th_name="นายสมชาย ใจดี",
            en_name="Mr. Somchai Jaidee",
            birth_date="1 มกราคม 2530",
            address="123 ถนนสุขุมวิท เขตเมือง จังหวัดนครราชสีมา 30000",
        )

    def measure_blood_pressure(self) -> BloodPressureReading:
        self._notify_measurement_progress("bp", None, started=True)
        time.sleep(1.2)
        return BloodPressureReading(
            systolic=random.randint(108, 132),
            diastolic=random.randint(68, 86),
            pulse=random.randint(66, 92),
        )

    def measure_spo2(self) -> int:
        final = random.randint(96, 100)
        samples = [max(90, final - 4), final - 2, final - 1, final, final]
        for index, value in enumerate(samples):
            time.sleep(0.18)
            self._notify_measurement_progress(
                "spo2",
                value,
                bpm=random.randint(66, 92),
                stable=index == len(samples) - 1,
                finger_detected=True,
            )
        return final

    def measure_temperature(self) -> float:
        final = round(random.uniform(36.2, 37.4), 1)
        samples = [final - 0.8, final - 0.4, final - 0.2, final, final]
        for index, value in enumerate(samples):
            time.sleep(0.18)
            self._notify_measurement_progress(
                "temp",
                round(value, 1),
                stable=index == len(samples) - 1,
                in_contact=True,
            )
        return final

    def get_device_status(self) -> DeviceStatus:
        self._battery_percent = max(10, self._battery_percent - random.choice([0, 0, 1]))
        return DeviceStatus(
            battery_percent=self._battery_percent,
            battery_charging=False,
            wifi_connected=True,
            wifi_disabled=_subsystem_disabled("wifi"),
            bp_disabled=_subsystem_disabled("bp_monitor"),
            spo2_disabled=_subsystem_disabled("spo2"),
            idcard_disabled=_subsystem_disabled("idcard"),
        )

    def get_measurement_history(self, patient_id: str) -> list[MeasurementHistoryRecord]:
        return [
            MeasurementHistoryRecord("24/06/69 12:00", 120, 78, 70, 98, 35.5),
            MeasurementHistoryRecord("24/06/69 11:55", 128, 89, 85, None, 36.6),
            MeasurementHistoryRecord("24/06/69 11:49", 119, 78, 80, 78, 35.5),
            MeasurementHistoryRecord("24/06/69 11:35", 120, 78, None, 76, 37.0),
        ]
    
    def send_data(self, payload: dict) -> bool:
        time.sleep(3.0)
        print("====== [Mock API Sent] ======")
        print(payload)
        print("=============================")
        return True

    def scan_wifi_networks(self) -> list[str]:
        time.sleep(0.5)
        return ["CareKeeper-Lab", "Hospital-WiFi", "Mobile-Hotspot"]

    def connect_wifi(self, ssid: str, password: str | None = None) -> bool:
        print(f"[Mock Wi-Fi] connect to {ssid}")
        return True

    def reboot_device(self) -> bool:
        print("[Mock] Reboot device")
        return True

    def shutdown_device(self) -> bool:
        print("[Mock] Shutdown device")
        return True

    def get_ip_address(self) -> str:
        return "192.168.1.123"


class RealCareKeeperProvider(CareKeeperProvider):
    """Hardware provider for Raspberry Pi / connected devices.

    This provider intentionally does not mock values. If a device is missing,
    the caller receives an exception so the GUI can show a clear error.
    """

    def __init__(
        self,
        bp_port: str | None = None,
        api_url: str | None = None,
        history_api_url: str | None = None,
    ) -> None:
        self.device_mac = read_device_mac()
        self.bp_port = bp_port or TEST_BP_PORT
        self.api_url = api_url or TEST_API_URL
        self.history_api_url = history_api_url or TEST_HISTORY_API_URL
        # Why the last blood-pressure measurement came back empty (a
        # BPMonitor.ERR_* code), or None after a good reading. The GUI reads
        # this to decide how long to lock the NIBP button: a device-reported
        # BP_ERROR means the cuff has put itself into its own lockout.
        self.last_bp_error: str | None = None
        # The cuff module's own error code behind a BP_ERROR, when the
        # firmware reports one. Kept next to last_bp_error so a failure
        # can be traced to a cause rather than to a category.
        self.last_bp_error_code: int | None = None

    def _notify_attempt(self, subsystem: str, attempt: int, max_attempts: int) -> None:
        if self.on_retry_attempt:
            self.on_retry_attempt(subsystem, attempt, max_attempts)

    def _notify_giveup(self, subsystem: str, reason: str) -> None:
        if self.on_retry_giveup:
            self.on_retry_giveup(subsystem, reason)

    def read_patient(self) -> PatientInfo:
        from lib.thaiidcard.card import ThaiIDCard

        info = retry_with_notify(
            ThaiIDCard().read,
            subsystem="idcard",
            on_attempt=lambda a, m: self._notify_attempt("idcard", a, m),
            on_give_up=lambda r: self._notify_giveup("idcard", r),
        )
        return PatientInfo(
            cid=info.cid,
            th_name=info.th_name,
            en_name=info.en_name,
            birth_date=info.birth_date,
            address=info.address,
        )

    # Seconds connect() gives the ESP32 bridge to finish booting before START
    # is sent (it returns sooner if the bridge announces READY). Class
    # attribute so tests can shrink it.
    _BP_BOOT_SETTLE_SECONDS = 3.0

    # Seconds to wait for a reading before calling the measurement failed. A
    # real cuff run (inflate + bleed down) takes 25-45 s, so this has to sit
    # above that -- past it the bridge is not answering, and every extra second
    # is just the operator standing there. Was 120, then 60; 50 clears the
    # slowest real run while keeping a dud well under a minute. Longer than the
    # other two probes on purpose: they can retry a settling window, a cuff run
    # cannot.
    _BP_MEASURE_TIMEOUT = 60

    # measure() reports WHY it came back empty; turn that into something the
    # operator can act on instead of one catch-all "ไม่สามารถอ่านค่าความดันได้".
    _BP_ERROR_MESSAGES = {
        "BP_ERROR": "เครื่องวัดความดันแจ้งข้อผิดพลาด (วัดไม่ติด/ผ้าพันแขนหลวม) กรุณารอประมาณ 2 นาทีแล้ววัดใหม่",
        # NOT_READY means the firmware still has the PREVIOUS run in progress
        # -- it is never sent for anything else. The wait depends on where it
        # is stuck, so _bp_not_ready_message() fills in the specific advice.
        "NOT_READY": "เครื่องวัดความดันยังทำงานรอบก่อนหน้าไม่เสร็จ กรุณารอสักครู่แล้ววัดใหม่",
        "TIMEOUT": "เครื่องวัดความดันไม่ตอบสนอง (ตรวจสอบสาย USB และสายผ้าพันแขน)",
        # NO_RESULT is not TIMEOUT: the cuff ran its full cycle and only
        # the reading went missing, so the module is in its post-run
        # lockout and the advice is to wait, not to check the cables.
        "NO_RESULT": "เครื่องวัดความดันวัดครบรอบแล้วแต่ไม่ได้ส่งค่ากลับมา กรุณารอสักครู่แล้ววัดใหม่ (หากเกิดซ้ำบ่อย ให้ตรวจสายสัญญาณระหว่างบอร์ดกับเครื่องวัด)",
    }

    def measure_blood_pressure(self) -> BloodPressureReading:
        from lib.bp_monitor import BPMonitor

        port = self._resolve_bp_port()
        if port != self.bp_port:
            print(f"[BP] auto-detected serial port: {port}")
        monitor = BPMonitor(
            port=port,
            timeout=self._BP_MEASURE_TIMEOUT,
            boot_settle_seconds=self._BP_BOOT_SETTLE_SECONDS,
            on_started=lambda: self._notify_measurement_progress(
                "bp", None, started=True
            ),
        )
        retry_with_notify(
            monitor.connect,
            subsystem="bp_monitor",
            on_attempt=lambda a, m: self._notify_attempt("bp_monitor", a, m),
            on_give_up=lambda r: self._notify_giveup("bp_monitor", r),
        )
        try:
            result = monitor.measure()
            reason = monitor.last_error
            busy_state = monitor.busy_state
            error_code = monitor.error_code
        finally:
            monitor.disconnect()

        self.last_bp_error = None if result else reason
        self.last_bp_error_code = None if result else error_code

        if not result:
            if reason == "NOT_READY":
                raise RuntimeError(self._bp_not_ready_message(busy_state))
            if reason == "BP_ERROR":
                raise RuntimeError(self._bp_device_error_message(error_code))
            raise RuntimeError(
                self._BP_ERROR_MESSAGES.get(reason, "ไม่สามารถอ่านค่าความดันได้")
            )

        return BloodPressureReading(
            systolic=result.sys,
            diastolic=result.dia,
            pulse=result.pul,
        )

    # What the module's own error code means. The module ends a bad run with
    # "end test,err:<n>" and the firmware forwards <n> as "BP_ERROR:<n>".
    # That number is the only thing separating "the arm moved" from "the cuff
    # is loose" from "the air line leaks" -- all three look identical to
    # everyone downstream of it.
    #
    # The vendor's table for this module is not in the repo, so nothing here
    # is guessed: every row was produced by making the failure happen on the
    # kiosk and reading the code back (sensor_tests/bp_error_codes.py, logged
    # in sensor_tests/bp-error-codes.log). A code with no row reaches the
    # operator as a number rather than as advice that might send them to fix
    # the wrong thing.
    _BP_DEVICE_CODE_MESSAGES: dict[int, str] = {
        # Negative codes are the firmware's own, not the module's.
        -1: "เครื่องวัดความดันหยุดตอบระหว่างวัด รอบวัดถูกตัดอัตโนมัติ กรุณาตรวจสายสัญญาณระหว่างบอร์ดกับเครื่องวัด แล้ววัดใหม่",
        # Observed 1 Sep 2026. 4 came back from BOTH 'cuff not on an arm'
        # and 'cuff wrapped loose' -- the module could not build or hold
        # pressure. The wording covers both, because the code does not
        # separate them and guessing which one it is helps nobody.
        4: "ผ้าพันแขนหลวมหรือยังไม่ได้พันแขน กรุณาพันผ้าพันแขนให้แน่นพอดี ให้ขอบล่างอยู่เหนือข้อพับแขนประมาณ 2 ซม. แล้ววัดใหม่",
        # Observed 1 Sep 2026 from moving the arm mid-run. Worded as "could
        # not read the signal, usually movement" rather than "you moved":
        # one run is enough to know movement produces this code, not enough
        # to know that nothing else does.
        6: "เครื่องวัดอ่านสัญญาณไม่ได้ (มักเกิดจากการขยับแขนหรือพูดคุยระหว่างวัด) กรุณานั่งนิ่ง วางแขนระดับหัวใจ แล้ววัดใหม่",
    }

    def _bp_device_error_message(self, code: int | None) -> str:
        """A BP_ERROR with the module's reason attached when we can name it.

        An unmapped code still goes on screen: a number the operator can
        write down and report beats a guess at what went wrong, and it is
        how _BP_DEVICE_CODE_MESSAGES gets filled in."""
        base = self._BP_ERROR_MESSAGES["BP_ERROR"]
        if code is None:
            return base
        if code in self._BP_DEVICE_CODE_MESSAGES:
            return self._BP_DEVICE_CODE_MESSAGES[code]
        return f"{base} (รหัสจากเครื่องวัด: {code})"

    # How long each stuck firmware state actually takes to clear, measured on
    # the kiosk: a run takes ~50 s, and the module then needs ~60 s more before
    # it powers down and the bridge reports READY.
    _BP_BUSY_MESSAGES = {
        "MEASURING": "เครื่องวัดความดันกำลังวัดรอบก่อนหน้าอยู่ กรุณารอให้ผ้าพันแขนคลายลมจนสุดก่อน (ประมาณ 1 นาที)",
        "WAIT_SHUTDOWN": "เครื่องวัดความดันกำลังปิดตัวเองหลังวัดรอบก่อน กรุณารอประมาณ 1 นาทีแล้ววัดใหม่",
        "TRIGGER": "เครื่องวัดความดันกำลังเริ่มรอบก่อนหน้า กรุณารอสักครู่แล้ววัดใหม่",
    }

    def _bp_not_ready_message(self, busy_state: str | None) -> str:
        """NOT_READY always means the PREVIOUS run has not finished -- the
        firmware sends it for nothing else. Newer firmware appends the state it
        is stuck in, which is the difference between "wait a moment" and "wait
        a minute", so say which one when it tells us."""
        if busy_state and busy_state in self._BP_BUSY_MESSAGES:
            return self._BP_BUSY_MESSAGES[busy_state]
        return self._BP_ERROR_MESSAGES["NOT_READY"]

    # USB vendor IDs of common USB-serial bridges (CH34x, CP210x, FTDI,
    # Prolific, Espressif native USB). Only used to PREFER the BP monitor's
    # ESP32 bridge when several USB-serial ports are present; detection still
    # falls back to the sole USB-serial port otherwise.
    _BP_USB_SERIAL_VIDS = {0x1A86, 0x10C4, 0x0403, 0x067B, 0x303A}

    def _resolve_bp_port(self) -> str:
        """Serial port of the BP monitor, auto-detected so it keeps working when
        the /dev/ttyUSB* number changes without editing .env.

        Precedence: an explicit CAREKEEPER_BP_PORT that is still plugged in wins;
        otherwise scan USB-serial adapters (the BP monitor is the only one on the
        kiosk), preferring a known ESP32 bridge chip, then the sole USB-serial
        port. Read-only -- it never opens or writes to a port, so it cannot
        trigger a real, cuff-inflating measurement."""
        from serial.tools import list_ports

        try:
            ports = list(list_ports.comports())
        except Exception:
            ports = []
        present = {p.device for p in ports}

        configured = (self.bp_port or "").strip()
        if configured and configured.lower() != "auto" and configured in present:
            return configured

        usb_ports = [p for p in ports if getattr(p, "vid", None) is not None]
        for p in usb_ports:
            if p.vid in self._BP_USB_SERIAL_VIDS:
                return p.device
        if usb_ports:
            return usb_ports[0].device

        if configured and configured.lower() != "auto":
            return configured  # let BPMonitor raise a clear "port not found"
        raise RuntimeError(
            "ไม่พบพอร์ตเครื่องวัดความดัน (ไม่มีอุปกรณ์ USB serial เสียบอยู่)"
        )

    # Seconds to keep polling the MAX30102 for a settled reading once the
    # sensor is open. The floor is ~9 s (4 s to fill the algorithm's window,
    # then 1 s per stability sample), and every lost-contact blip throws the
    # window away and costs that 9 s again -- so this budget allows roughly
    # three settling attempts. Matches the temperature probe; the BP monitor
    # gets longer because a cuff run cannot be retried mid-flight. Class
    # attribute so tests can shrink it.
    _SPO2_READ_TIMEOUT = 30.0

    # Why the settling loop gave up, in words the operator can act on.
    _SPO2_ERROR_MESSAGES = {
        "NO_FINGER": "ไม่พบนิ้วบนเซนเซอร์ SpO2 (วางนิ้วให้แนบเต็มหน้าเซนเซอร์)",
        "WEAK_SIGNAL": "สัญญาณ SpO2 อ่อนเกินไป (วางนิ้วให้แนบสนิท ไม่กดแรง และอยู่นิ่งๆ)",
        "UNSTABLE": "ค่า SpO2 ยังไม่นิ่ง (อยู่นิ่งๆ อย่าขยับนิ้วระหว่างวัด)",
    }

    def measure_spo2(self) -> int:
        # SpO2 comes from a MAX30102 over I2C. Only OPENING the sensor is
        # retried/disabled (a real hardware
        # fault); the read then polls until the reading settles, so a
        # finger-not-placed timeout doesn't disable the subsystem. Mirrors the
        # BP monitor's connect-vs-measure split.
        monitor = retry_with_notify(
            self._open_spo2_sensor,
            subsystem="spo2",
            on_attempt=lambda a, m: self._notify_attempt("spo2", a, m),
            on_give_up=lambda r: self._notify_giveup("spo2", r),
        )
        try:
            return self._read_spo2(monitor)
        finally:
            try:
                monitor.m.shutdown()
            except Exception:
                pass

    def _open_spo2_sensor(self):
        from lib.spo2_max30102 import SpO2Monitor

        return SpO2Monitor(max_wait_seconds=self._SPO2_READ_TIMEOUT)

    def _read_spo2(self, monitor) -> int:
        # The monitor settles the value itself -- it keeps sliding its sample
        # window until several consecutive estimates agree, then returns the
        # median of that window -- exactly like the DS18B20 probe settles body
        # temperature. So what reaches the GUI is a final reading, not the
        # first in-range sample that happened to come out of the algorithm.
        # Values outside 70-100% and finger-off windows are rejected inside
        # measure_spo2(), which returns None if nothing ever settles.
        result = monitor.measure_spo2(
            on_progress=lambda spo2, **state: self._notify_measurement_progress(
                "spo2", spo2, **state
            )
        )
        if result is None:
            raise RuntimeError(self._spo2_failure_message(monitor))
        return int(result)

    def _spo2_failure_message(self, monitor) -> str:
        """Say WHICH of the three ways it failed, and show the IR level.

        "no finger" and "finger there but the value never settled" call for
        opposite things from the operator, and one catch-all message asked for
        both. The IR DC reading is appended because a finger that reads as
        absent is how a mis-tuned FINGER_IR_THRESHOLD presents -- that number
        is what CAREKEEPER_SPO2_FINGER_IR_THRESHOLD gets set against."""
        reason = getattr(monitor, "last_error", None)
        message = self._SPO2_ERROR_MESSAGES.get(
            reason, "อ่านค่า SpO2 ไม่สำเร็จ (วางนิ้วให้แนบเซนเซอร์แล้วอยู่นิ่งๆ)"
        )
        ir_dc = getattr(monitor, "last_ir_dc", None)
        if reason == "NO_FINGER" and ir_dc is not None:
            threshold = getattr(monitor, "finger_ir_threshold", None)
            message += f" [IR={ir_dc}/{threshold}]"
        overflows = getattr(monitor, "overflows", 0)
        if overflows:
            message += f" (สัญญาณขาดช่วง {overflows} ครั้ง)"
        return message

    # Seconds the DS18B20 gets to settle before the measurement is called
    # failed, same budget as the other two probes. Passed in rather than left
    # to temp_sensor's own default so all three timeouts live together here.
    _TEMP_READ_TIMEOUT = 30.0

    def measure_temperature(self) -> float:
        # Contact body-temp probe (DS18B20, 1-Wire) from the E-Medhealth library.
        from lib.temp_sensor import temp_sensor

        try:
            sensor = temp_sensor(max_wait_seconds=self._TEMP_READ_TIMEOUT)
        except Exception as e:
            raise RuntimeError(f"ไม่พบเซนเซอร์อุณหภูมิ (DS18B20): {e}")

        result = sensor.measure_body_temperature(
            on_progress=lambda temp, **state: self._notify_measurement_progress(
                "temp", temp, **state
            )
        )
        if result is None:
            raise RuntimeError("วัดอุณหภูมิไม่สำเร็จ (แนบเซนเซอร์กับผิวแล้วรอให้ค่านิ่ง)")
        return float(result)

    def get_device_status(self) -> DeviceStatus:
        battery_percent, battery_charging = self._read_battery_state()
        return DeviceStatus(
            battery_percent=battery_percent,
            battery_charging=battery_charging,
            wifi_connected=self._is_wifi_connected(),
            wifi_disabled=_subsystem_disabled("wifi"),
            bp_disabled=_subsystem_disabled("bp_monitor"),
            spo2_disabled=_subsystem_disabled("spo2"),
            idcard_disabled=_subsystem_disabled("idcard"),
        )

    def get_measurement_history(self, patient_id: str) -> list[MeasurementHistoryRecord]:
        if not self.history_api_url:
            return []

        headers = {"Content-Type": "application/json"}
        if TEST_API_KEY:
            headers[TEST_API_KEY_HEADER] = TEST_API_KEY

        response = requests.get(
            self.history_api_url,
            params={
                TEST_HISTORY_PATIENT_ID_PARAM: patient_id,
                TEST_HISTORY_MAC_PARAM: self.device_mac,
                "limit": 4,
            },
            headers=headers,
            timeout=8,
        )

        if not (200 <= response.status_code < 300):
            raise RuntimeError(f"ดึงข้อมูลย้อนหลังไม่สำเร็จ Status Code: {response.status_code}")

        raw = response.json()

        if isinstance(raw, dict):
            items = raw.get("data") or raw.get("records") or raw.get("history") or []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []

        records: list[MeasurementHistoryRecord] = []

        for item in items[:4]:
            if not isinstance(item, dict):
                continue

            records.append(
                MeasurementHistoryRecord(
                    measured_at=(
                        item.get("measured_at")
                        or item.get("date")
                        or item.get("created_at")
                        or "-"
                    ),
                    systolic=item.get("sys") if item.get("sys") is not None else item.get("systolic"),
                    diastolic=item.get("dia") if item.get("dia") is not None else item.get("diastolic"),
                    pulse=(
                        item.get("pulse")
                        if item.get("pulse") is not None
                        else item.get("pr_bpm") if item.get("pr_bpm") is not None
                        else item.get("heart_rate")
                    ),
                    spo2=item.get("spo2"),
                    temperature=item.get("temperature") if item.get("temperature") is not None else item.get("temp"),
                )
            )

        return records

    # The UPS HAT is re-read on every status tick, so a permanently broken I2C
    # bus would print the same line forever; keep the first reason only.
    _battery_error_logged = False

    def _read_battery_state(self) -> tuple[int | None, bool]:
        try:
            from lib.ups import UPSHat

            ups = UPSHat()
            percent = int(ups.get_battery_percent())
            charging = ups.get_status() in {
                UPSHat.STATUS_CHARGING,
                UPSHat.STATUS_FAST_CHARGING,
            }
            return percent, charging
        except Exception as e:
            # The GUI can only show "--%" and the not-charging mark, which does
            # not say whether the HAT is missing, I2C is off, or smbus failed to
            # import. Print the real reason once without flooding every poll.
            if not self._battery_error_logged:
                self._battery_error_logged = True
                print(f"[Battery] read failed, showing '--%': {type(e).__name__}: {e}")
            return None, False

    def _is_wifi_connected(self) -> bool:
        if sys.platform == "win32":
            try:
                output = subprocess.check_output(
                    ["netsh", "wlan", "show", "interfaces"],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    text=True,
                    errors="ignore",
                    timeout=4,
                )
                return "State" in output and "connected" in output.lower()
            except Exception:
                return False

        try:
            output = subprocess.check_output(["iwgetid", "-r"], text=True, errors="ignore", timeout=3)
            return bool(output.strip())
        except Exception:
            return False

    def send_data(self, payload: dict) -> bool:
        if not self.api_url:
            raise RuntimeError("ยังไม่ได้ตั้งค่า CAREKEEPER_API_URL สำหรับ backend")

        headers = {"Content-Type": "application/json"}
        if TEST_API_KEY:
            headers[TEST_API_KEY_HEADER] = TEST_API_KEY
        
        response = requests.post(self.api_url, json=payload, headers=headers, timeout=8)
        
        if 200 <= response.status_code < 300:
            return True

        raise RuntimeError(f"Server ปฏิเสธข้อมูล (Status Code: {response.status_code})")

    def toggle_wifi(self) -> None:
        current_state = self._is_wifi_connected()
        cmd = "off" if current_state else "on"
        try:
            subprocess.run(["nmcli", "radio", "wifi", cmd], check=True, timeout=6)
        except Exception as e:
            print(f"Failed to toggle WiFi: {e}")

    def scan_wifi_networks(self) -> list[str]:
        return retry_with_notify(
            self._scan_wifi_networks_once,
            subsystem="wifi",
            on_attempt=lambda a, m: self._notify_attempt("wifi", a, m),
            on_give_up=lambda r: self._notify_giveup("wifi", r),
        )

    def _scan_wifi_networks_once(self) -> list[str]:
        # Force a fresh scan first so the list reflects every AP currently in
        # range, not just whatever NetworkManager happened to have cached from
        # its last periodic scan (a just-appeared network would otherwise be
        # missing). `--rescan no` on the list then reads that fresh cache without
        # triggering a second scan NM would reject for coming too soon.
        self._rescan_wifi()
        try:
            output = subprocess.check_output(
                ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "no"],
                text=True,
                errors="ignore",
                timeout=10,
            )
            networks = []
            for line in output.splitlines():
                ssid = line.strip()
                if ssid and ssid not in networks:
                    networks.append(ssid)
            return networks
        except subprocess.TimeoutExpired:
            raise RuntimeError("สแกน Wi-Fi ใช้เวลานานเกินไป")
        except Exception as e:
            raise RuntimeError(f"ไม่สามารถสแกน Wi-Fi ได้: {e}")

    def connect_wifi(self, ssid: str, password: str | None = None) -> bool:
        return retry_with_notify(
            lambda: self._connect_wifi_once(ssid, password),
            subsystem="wifi",
            on_attempt=lambda a, m: self._notify_attempt("wifi", a, m),
            on_give_up=lambda r: self._notify_giveup("wifi", r),
        )

    def _connect_wifi_once(self, ssid: str, password: str | None = None) -> bool:
        # Remember the WiFi connection currently in use before we tear it down,
        # so a failed attempt can put it back. `nmcli device wifi connect`
        # deactivates the active link first; if the new one then fails the Pi is
        # left with no network at all, killing the SSH/VNC session used to
        # administer it (and leaving no way to even read `hostname -I`).
        previous = self._active_wifi_connection()

        # `nmcli device wifi connect` reactivates any saved profile for this
        # SSID using its *stored* password and silently ignores the new one we
        # pass -- so once the router password changes, the stale profile can
        # never authenticate again. Delete it first so the password the user
        # just entered is the one actually used.
        if password:
            self._forget_wifi_profile(ssid)

        # A radio that was just switched on has an empty scan cache, so NM
        # cannot see the AP nor its security type and rejects the connect with
        # "802-11-wireless-security.key-mgmt: property is missing". Refresh the
        # cache first (best-effort) so a visible AP is known before we connect.
        self._rescan_wifi()
        try:
            self._nmcli_wifi_connect(ssid, password, hidden=False)
            return True
        except subprocess.TimeoutExpired:
            self._restore_wifi_connection(previous, ssid)
            raise RuntimeError("เชื่อมต่อ Wi-Fi ใช้เวลานานเกินไป")
        except subprocess.CalledProcessError as e:
            message = self._nmcli_error_message(e)
            # Hidden APs never broadcast their SSID, so even after a rescan NM
            # can't infer the security type -> same key-mgmt error. Retry once
            # flagging the network as hidden; NM then probes for it actively and
            # derives WPA-PSK from the supplied password itself.
            if password and "key-mgmt" in message.lower():
                try:
                    self._nmcli_wifi_connect(ssid, password, hidden=True)
                    return True
                except subprocess.TimeoutExpired:
                    self._restore_wifi_connection(previous, ssid)
                    raise RuntimeError("เชื่อมต่อ Wi-Fi ใช้เวลานานเกินไป")
                except subprocess.CalledProcessError as e2:
                    message = self._nmcli_error_message(e2)
            self._restore_wifi_connection(previous, ssid)
            raise RuntimeError(f"เชื่อมต่อ Wi-Fi ไม่สำเร็จ: {message}")

    def _active_wifi_connection(self) -> str | None:
        """Name of the Wi-Fi connection profile currently active, if any, so it
        can be restored after a failed connect. Best-effort: returns None when
        nothing is up or nmcli is unavailable."""
        try:
            output = subprocess.check_output(
                ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
                text=True, errors="ignore", timeout=5,
            )
        except Exception:
            return None
        for line in output.splitlines():
            # `-t` output is colon-separated (NAME:TYPE); a literal colon inside
            # NAME is escaped as "\:", so the real separator is the last colon.
            name, sep, ctype = line.rpartition(":")
            if sep and ctype == "802-11-wireless" and name:
                return name.replace("\\:", ":")
        return None

    def _forget_wifi_profile(self, ssid: str) -> None:
        """Delete any saved NM profile named after this SSID so a changed
        password takes effect (see `_connect_wifi_once`). Best-effort: a missing
        profile just makes nmcli exit non-zero, which we ignore."""
        try:
            subprocess.run(
                ["nmcli", "connection", "delete", "id", ssid],
                check=False, capture_output=True, text=True, timeout=8,
            )
        except Exception:
            pass

    def _restore_wifi_connection(self, name: str | None, failed_ssid: str) -> None:
        """Bring the previously active Wi-Fi connection back up after a failed
        connect, so the device is never left offline (which would kill the
        SSH/VNC session used to administer it). No-op when there was no previous
        connection, or when it is the very network we just failed to join."""
        if not name or name == failed_ssid:
            return
        try:
            subprocess.run(
                ["nmcli", "connection", "up", "id", name],
                check=False, capture_output=True, text=True, timeout=20,
            )
        except Exception:
            pass

    def _rescan_wifi(self) -> None:
        """Force NetworkManager to refresh its scan results before a connect.
        `--rescan yes` blocks until the scan completes, so the AP and its
        security flags are known by the time we connect. Best-effort: NM
        rejects a rescan that follows too soon after a previous one, which is
        harmless here, so any failure is ignored."""
        try:
            subprocess.run(
                ["nmcli", "device", "wifi", "list", "--rescan", "yes"],
                check=False, text=True, capture_output=True, timeout=10,
            )
        except Exception:
            pass

    def _nmcli_wifi_connect(self, ssid: str, password: str | None, hidden: bool) -> None:
        command = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            command.extend(["password", password])
        if hidden:
            command.extend(["hidden", "yes"])
        subprocess.run(command, check=True, text=True, capture_output=True, timeout=25)

    @staticmethod
    def _nmcli_error_message(exc: subprocess.CalledProcessError) -> str:
        return (exc.stderr or "").strip() or (exc.stdout or "").strip() or str(exc)

    def reboot_device(self) -> bool:
        try:
            subprocess.run(["systemctl", "reboot"], check=True, capture_output=True, text=True, timeout=5)
            return True
        except subprocess.CalledProcessError as e:
            message = e.stderr.strip() or e.stdout.strip() or str(e)
            raise RuntimeError(f"รีสตาร์ทเครื่องไม่สำเร็จ: {message}")
        except Exception as e:
            raise RuntimeError(f"รีสตาร์ทเครื่องไม่สำเร็จ: {e}")

    def shutdown_device(self) -> bool:
        try:
            subprocess.run(["systemctl", "poweroff"], check=True, capture_output=True, text=True, timeout=5)
            return True
        except subprocess.CalledProcessError as e:
            message = e.stderr.strip() or e.stdout.strip() or str(e)
            raise RuntimeError(f"ปิดเครื่องไม่สำเร็จ: {message}")
        except Exception as e:
            raise RuntimeError(f"ปิดเครื่องไม่สำเร็จ: {e}")

    def get_ip_address(self) -> str:
        """Current IP address(es) from `hostname -I`, shown in the on-screen
        'ดู ID' dialog so the operator can find the device to SSH/VNC into after
        the network changes. Space-separated (IPv4 first); '' when offline."""
        if sys.platform == "win32":
            return ""
        try:
            output = subprocess.check_output(
                ["hostname", "-I"], text=True, errors="ignore", timeout=4
            )
            return output.strip()
        except Exception:
            return ""
