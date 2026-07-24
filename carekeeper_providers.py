# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
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

from carekeeper_retry import retry_with_notify, retry_with_notify_async, SubsystemRegistry


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
TEST_H59_DEVICE_NAME = _env("CAREKEEPER_H59_DEVICE_NAME")
TEST_H59_DEVICE_ADDRESS = _env("CAREKEEPER_H59_DEVICE_ADDRESS")
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
    wifi_connected: bool = False
    bluetooth_connected: bool = False
    wifi_disabled: bool = False
    bluetooth_disabled: bool = False
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

    def scan_bluetooth_devices(self) -> list[tuple[str, str]]:
        return []

    def connect_bluetooth(self, address: str) -> bool:
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
        time.sleep(1.2)
        return BloodPressureReading(
            systolic=random.randint(108, 132),
            diastolic=random.randint(68, 86),
            pulse=random.randint(66, 92),
        )

    def measure_spo2(self) -> int:
        time.sleep(0.9)
        return random.randint(96, 100)

    def measure_temperature(self) -> float:
        time.sleep(0.9)
        return round(random.uniform(36.2, 37.4), 1)

    def get_device_status(self) -> DeviceStatus:
        self._battery_percent = max(10, self._battery_percent - random.choice([0, 0, 1]))
        return DeviceStatus(
            battery_percent=self._battery_percent,
            wifi_connected=True,
            bluetooth_connected=True,
            wifi_disabled=_subsystem_disabled("wifi"),
            bluetooth_disabled=_subsystem_disabled("bluetooth"),
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

    def scan_bluetooth_devices(self) -> list[tuple[str, str]]:
        time.sleep(0.5)
        return [("H59_D105", TEST_H59_DEVICE_ADDRESS), ("Demo Oximeter", "AA:BB:CC:DD:EE:FF")]

    def connect_bluetooth(self, address: str) -> bool:
        print(f"[Mock Bluetooth] connect to {address}")
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
        h59_device_name: str | None = None,
        h59_device_address: str | None = None,
        api_url: str | None = None,
        history_api_url: str | None = None,
    ) -> None:
        self.device_mac = read_device_mac()
        self.bp_port = bp_port or TEST_BP_PORT
        self.h59_device_name = h59_device_name or TEST_H59_DEVICE_NAME
        self.h59_device_address = h59_device_address or TEST_H59_DEVICE_ADDRESS
        self.api_url = api_url or TEST_API_URL
        self.history_api_url = history_api_url or TEST_HISTORY_API_URL

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

    def measure_blood_pressure(self) -> BloodPressureReading:
        from lib.bp_monitor import BPMonitor

        monitor = BPMonitor(port=self.bp_port, timeout=120)
        retry_with_notify(
            monitor.connect,
            subsystem="bp_monitor",
            on_attempt=lambda a, m: self._notify_attempt("bp_monitor", a, m),
            on_give_up=lambda r: self._notify_giveup("bp_monitor", r),
        )
        try:
            result = monitor.measure()
        finally:
            monitor.disconnect()

        if not result:
            raise RuntimeError("ไม่สามารถอ่านค่าความดันได้")

        return BloodPressureReading(
            systolic=result.sys,
            diastolic=result.dia,
            pulse=result.pul,
        )

    def measure_spo2(self) -> int:
        return asyncio.run(self._measure_spo2_async())

    async def _measure_spo2_async(self) -> int:
        from lib.h59_ble import H59Device, SpO2Reader

        device = H59Device(
            device_name=self.h59_device_name,
            device_address=self.h59_device_address,
        )
        reader = SpO2Reader(device, timeout=60)

        try:
            # max_retries=1 here means each outer attempt makes exactly one
            # connect try; retry_with_notify_async is the sole source of the
            # 3-attempts-then-disable count, so the two retry layers don't compound.
            await retry_with_notify_async(
                lambda: device.connect(max_retries=1),
                subsystem="spo2",
                on_attempt=lambda a, m: self._notify_attempt("spo2", a, m),
                on_give_up=lambda r: self._notify_giveup("spo2", r),
            )
            value = await reader.read()
            if value is None:
                raise RuntimeError("ไม่สามารถอ่านค่า SpO2 ได้")
            return int(value)
        finally:
            reader.close()
            if device.is_connected:
                await device.disconnect()

    def measure_temperature(self) -> float:
        raise NotImplementedError("ยังไม่ได้เชื่อมต่อโมดูลวัดอุณหภูมิจริง")

    def get_device_status(self) -> DeviceStatus:
        return DeviceStatus(
            battery_percent=self._read_battery_percent(),
            wifi_connected=self._is_wifi_connected(),
            bluetooth_connected=self._is_bluetooth_connected(),
            wifi_disabled=_subsystem_disabled("wifi"),
            bluetooth_disabled=_subsystem_disabled("bluetooth"),
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

    def _read_battery_percent(self) -> int | None:
        try:
            from lib.ups import UPSHat

            return int(UPSHat().get_battery_percent())
        except Exception:
            return None

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

    def _is_bluetooth_connected(self) -> bool:
        try:
            output = subprocess.check_output(
                ["bluetoothctl", "devices", "Connected"],
                text=True,
                errors="ignore",
                timeout=4,
            )
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
        try:
            output = subprocess.check_output(
                ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"],
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

    def toggle_bluetooth(self) -> None:
        current_state = self._is_bluetooth_connected()
        cmd = "power off" if current_state else "power on"
        try:
            subprocess.run(["bluetoothctl", cmd.split()[0], cmd.split()[1]], check=True, timeout=6)
        except Exception as e:
            print(f"Failed to toggle Bluetooth: {e}")

    def scan_bluetooth_devices(self) -> list[tuple[str, str]]:
        return retry_with_notify(
            self._scan_bluetooth_devices_once,
            subsystem="bluetooth",
            on_attempt=lambda a, m: self._notify_attempt("bluetooth", a, m),
            on_give_up=lambda r: self._notify_giveup("bluetooth", r),
        )

    def _scan_bluetooth_devices_once(self) -> list[tuple[str, str]]:
        try:
            raw = asyncio.run(self._discover_ble_devices())
        except Exception as e:
            raise RuntimeError(f"ไม่สามารถสแกน Bluetooth ได้: {e}")
        return self._clean_ble_devices(raw)

    async def _discover_ble_devices(self) -> list[tuple[str | None, str | None, int | None]]:
        """Scan for BLE devices with bleak -- the same stack the H59 reader uses.

        `bluetoothctl devices` lists the adapter's entire ever-seen cache and
        renders unnamed devices as a bare MAC, which is why the picker showed
        ~10 stale/unidentifiable signals when only a couple were actually
        nearby. `BleakScanner.discover` returns only devices advertising during
        this window, each with its advertised name and RSSI."""
        from bleak import BleakScanner

        found = await BleakScanner.discover(timeout=8.0, return_adv=True)
        return [
            ((adv.local_name or device.name), device.address, adv.rssi)
            for device, adv in found.values()
        ]

    @staticmethod
    def _clean_ble_devices(
        raw: list[tuple[str | None, str | None, int | None]]
    ) -> list[tuple[str, str]]:
        """Turn raw scan results into a clean (name, address) picklist.

        Drops devices with no advertised name -- their label would just be the
        MAC address, which the operator can't identify and is exactly the noise
        being complained about -- then orders by signal strength so the
        physically closest device comes first. De-dupes by address."""
        by_strength: list[tuple[int, str, str]] = []
        for name, address, rssi in raw:
            name = (name or "").strip()
            address = (address or "").strip()
            if not address:
                continue
            # An unnamed device reports its MAC as the "name" (colon- or
            # dash-separated). Treat that as no name and skip it.
            if not name or name.replace("-", ":").upper() == address.upper():
                continue
            by_strength.append((rssi if rssi is not None else -999, name, address))

        by_strength.sort(key=lambda t: t[0], reverse=True)  # strongest first

        seen: set[str] = set()
        devices: list[tuple[str, str]] = []
        for _rssi, name, address in by_strength:
            key = address.upper()
            if key not in seen:
                seen.add(key)
                devices.append((name, address))
        return devices

    def connect_bluetooth(self, address: str) -> bool:
        return retry_with_notify(
            lambda: self._connect_bluetooth_once(address),
            subsystem="bluetooth",
            on_attempt=lambda a, m: self._notify_attempt("bluetooth", a, m),
            on_give_up=lambda r: self._notify_giveup("bluetooth", r),
        )

    def _connect_bluetooth_once(self, address: str) -> bool:
        try:
            subprocess.run(["bluetoothctl", "pair", address], timeout=15, capture_output=True, text=True)
            subprocess.run(["bluetoothctl", "trust", address], timeout=10, capture_output=True, text=True)
            subprocess.run(["bluetoothctl", "connect", address], timeout=15, check=True, capture_output=True, text=True)
            self.h59_device_address = address
            return True
        except subprocess.TimeoutExpired:
            raise RuntimeError("เชื่อมต่อ Bluetooth ใช้เวลานานเกินไป")
        except subprocess.CalledProcessError as e:
            message = e.stderr.strip() or e.stdout.strip() or str(e)
            raise RuntimeError(f"เชื่อมต่อ Bluetooth ไม่สำเร็จ: {message}")

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
