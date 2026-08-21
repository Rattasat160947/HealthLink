# bp_monitor.py

import serial
import threading
import time
from typing import Optional, Callable
from dataclasses import dataclass

@dataclass
class BPResult:
    sys: int   # Systolic  (mmHg)
    dia: int   # Diastolic (mmHg)
    pul: int   # Pulse     (bpm)

    def __str__(self):
        return (f"SYS: {self.sys} mmHg | "
                f"DIA: {self.dia} mmHg | "
                f"PUL: {self.pul} bpm")


class BPMonitor:
    """
    Class library สำหรับควบคุมเครื่องวัดความดัน AC21CN3508
    ผ่าน ESP32 ด้วย Serial UART

    ตัวอย่างการใช้งาน:
        bp = BPMonitor(port="COM3")
        bp.connect()
        result = bp.measure()
        if result:
            print(result)
        bp.disconnect()
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 120.0,
        on_ready:  Optional[Callable] = None,
        on_result: Optional[Callable[[BPResult], None]] = None,
        on_error:  Optional[Callable[[str], None]] = None,
    ):
        
        self.port      = port
        self.baudrate  = baudrate
        self.timeout   = timeout
        self.on_ready  = on_ready
        self.on_result = on_result
        self.on_error  = on_error

        self._ser: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # status
        self._is_ready   = True   
        self._last_result: Optional[BPResult] = None
        self._last_error: Optional[str] = None

        self._done_event = threading.Event()

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def connect(self):
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=1
        )
        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True
        )
        self._thread.start()
        print(f"[BPMonitor] Connected to {self.port}")

    def disconnect(self):
        self._running = False
        if self._ser and self._ser.is_open:
            self._ser.close()
        print("[BPMonitor] Disconnected")

    def measure(self, blocking: bool = True) -> Optional[BPResult]:

        if not self._is_ready:
            print("[BPMonitor] NOT READY")
            return None

        self._last_result = None
        self._last_error  = None
        self._done_event.clear()
        self._is_ready = False

        self._send("START")
        print("[BPMonitor] CMD Sending --> START waiting for result...")

        if not blocking:
            return None

        finished = self._done_event.wait(timeout=self.timeout)

        if not finished:
            print("[BPMonitor] Timeout — BP measurement fail")
            return None

        return self._last_result

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    @property
    def last_result(self) -> Optional[BPResult]:
        return self._last_result

    # ─────────────────────────────────────────
    # Internal
    # ─────────────────────────────────────────
    def _send(self, msg: str):
        if self._ser and self._ser.is_open:
            self._ser.write((msg + "\n").encode())

    def _read_loop(self):
        while self._running:
            try:
                if self._ser and self._ser.in_waiting:
                    line = self._ser.readline().decode(errors="ignore").strip()
                    if line:
                        self._handle_line(line)
            except serial.SerialException as e:
                print(f"[BPMonitor] Serial error: {e}")
                break
            except Exception as e:
                print(f"[BPMonitor] Error: {e}")

    def _handle_line(self, line: str):
        if line.startswith("SYS:"):
            result = self._parse_result(line)
            if result:
                self._last_result = result
                print(f"\n{'='*40}")
                print(f"  SYS : {result.sys:3d} mmHg")
                print(f"  DIA : {result.dia:3d} mmHg")
                print(f"  PUL : {result.pul:3d} bpm")
                print(f"{'='*40}\n")
                if self.on_result:
                    self.on_result(result)

        # ── Error: "BP_ERROR" ──
        elif line == "BP_ERROR":
            self._last_error = "BP_ERROR"
            print("\n[BPMonitor] Measurement Error — please wait 2 min\n")
            if self.on_error:
                self.on_error("BP_ERROR")

        elif line == "READY":
            self._is_ready = True
            self._done_event.set()   
            print("[BPMonitor] READY !! ")
            if self.on_ready:
                self.on_ready()

        elif line == "NOT_READY":
            print("[BPMonitor] NOT_READY")
            self._done_event.set()

    @staticmethod
    def _parse_result(line: str) -> Optional[BPResult]:
        """COnvert 'SYS:89,DIA:76,PUL:49' → BPResult"""
        try:
            parts = {}
            for item in line.split(","):
                k, v = item.split(":")
                parts[k.strip()] = int(v.strip())
            return BPResult(
                sys=parts["SYS"],
                dia=parts["DIA"],
                pul=parts["PUL"]
            )
        except Exception:
            return None
        
if __name__ == "__main__":
    bp = BPMonitor(port="/dev/ttyUSB0")  # Windows: "COM3" / Linux: "/dev/ttyUSB0"
    bp.connect()
    result = bp.measure()

    if result:
        print(f"result: {result}")
    else:
        print("BP Measurement fail")

    bp.disconnect()
