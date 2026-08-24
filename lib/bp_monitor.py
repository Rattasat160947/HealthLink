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

    # Reasons stored on last_error when measure() comes back empty, so the
    # caller can tell "the cuff reported a fault" apart from "nothing answered".
    ERR_DEVICE    = "BP_ERROR"
    ERR_NOT_READY = "NOT_READY"
    ERR_TIMEOUT   = "TIMEOUT"

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 60.0,
        boot_settle_seconds: float = 3.0,
        reset_on_connect: bool = True,
        on_ready:  Optional[Callable] = None,
        on_result: Optional[Callable[[BPResult], None]] = None,
        on_error:  Optional[Callable[[str], None]] = None,
    ):

        self.port      = port
        self.baudrate  = baudrate
        self.timeout   = timeout
        self.boot_settle_seconds = boot_settle_seconds
        self.reset_on_connect = reset_on_connect
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
        # Which firmware state a NOT_READY came from, when it says.
        self.busy_state: Optional[str] = None

        self._done_event = threading.Event()
        self._ready_event = threading.Event()
        self._partial = b""   # leftover bytes of a line split across two reads

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def connect(self):
        self._ser = self._open_port()
        self._running = True
        self._ready_event.clear()
        self._partial = b""
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True
        )
        self._thread.start()

        # Opening the port does NOT reset this board -- verified on the kiosk:
        # a START right after open() came back NOT_READY, which only happens
        # when the firmware is still in a state left over from a previous
        # session. So the firmware's state machine survives the app being
        # restarted, and a run that was interrupted keeps every later START
        # answered with NOT_READY until the module finishes and powers down
        # (~50 s of measuring, then ~60 s to power down -- the two minutes).
        #
        # RESET puts it back to IDLE and is answered with READY. Firmware that
        # predates the command ignores it silently, so this is safe to send to
        # a board that has not been reflashed yet -- it just gets no answer,
        # and the wait below falls through as it always did.
        self._send("RESET")
        if self.boot_settle_seconds > 0:
            self._ready_event.wait(timeout=self.boot_settle_seconds)

        if self._ready_event.is_set():
            print(f"[BPMonitor] Connected to {self.port} (bridge is READY)")
        else:
            print(
                f"[BPMonitor] Connected to {self.port} -- no answer to RESET "
                f"in {self.boot_settle_seconds:.0f}s (firmware without RESET "
                "support?); measuring anyway"
            )

    def disconnect(self):
        self._running = False
        thread, self._thread = self._thread, None
        # Let the reader come back out of its blocking read BEFORE the port is
        # closed. The provider builds a fresh BPMonitor per measurement, so a
        # reader still holding the old handle makes the next connect() fail
        # with "device or resource busy".
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if self._ser and self._ser.is_open:
            self._ser.close()
        print("[BPMonitor] Disconnected")

    def measure(self, blocking: bool = True) -> Optional[BPResult]:

        if not self._is_ready:
            print("[BPMonitor] NOT READY")
            self._last_error = self.ERR_NOT_READY
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
            self._last_error = self.ERR_TIMEOUT
            print("[BPMonitor] Timeout — BP measurement fail")
            return None

        return self._last_result

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    @property
    def last_result(self) -> Optional[BPResult]:
        return self._last_result

    @property
    def last_error(self) -> Optional[str]:
        """Why the last measure() came back empty: ERR_DEVICE (the cuff
        reported a fault), ERR_NOT_READY, or ERR_TIMEOUT (nothing answered)."""
        return self._last_error

    # ─────────────────────────────────────────
    # Internal
    # ─────────────────────────────────────────
    def _open_port(self) -> serial.Serial:
        """Open the port and let the ESP32 bridge reboot as it always has.

        Opening a port asserts DTR/RTS, and on CH34x / CP210x boards those
        drive the ESP32's auto-reset circuit, so the bridge reboots. That
        reboot is deliberately kept: it is what gives every measurement a
        freshly booted firmware instead of whatever state the last one left
        behind, and the cuff's own error path parks the firmware in a state
        worth clearing. What was actually broken was never the reset -- it was
        sending START milliseconds later, into a bootloader that swallows it,
        and then waiting out the whole timeout for a reply nobody was going to
        send. connect() waits for the boot to finish instead.

        reset_on_connect=False deasserts both lines first, which keeps running
        firmware running (also clearing HUPCL so close() does not drop DTR
        either). Kept for boards whose firmware is slow to come up, but it
        means a wedged firmware stays wedged, so it is not the default."""
        ser = serial.Serial()
        ser.port     = self.port
        ser.baudrate = self.baudrate
        ser.timeout  = 1
        if not self.reset_on_connect:
            try:
                ser.dtr = False
                ser.rts = False
            except Exception:
                pass  # not every platform/backend accepts these before open()
        ser.open()
        if not self.reset_on_connect:
            self._disable_hupcl(ser)
        try:
            ser.reset_input_buffer()  # drop anything stale from a past session
        except Exception:
            pass
        return ser

    @staticmethod
    def _disable_hupcl(ser) -> None:
        """Stop the kernel from dropping DTR when this port is closed (POSIX).

        HUPCL is on by default, so close() lowers the modem lines — which is
        another way to reset the ESP32 bridge. The provider opens and closes a
        port per measurement, so with HUPCL left on, every measurement rebooted
        the bridge for the next one. No-op on Windows and on fakes."""
        fd = getattr(ser, "fd", None)
        if fd is None:
            return
        try:
            import termios

            attrs = termios.tcgetattr(fd)
            attrs[2] &= ~termios.HUPCL  # cflag
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass  # not POSIX, or the driver refuses — the boot settle covers it

    def _send(self, msg: str):
        if self._ser and self._ser.is_open:
            self._ser.write((msg + "\n").encode())

    def _read_loop(self):
        while self._running:
            try:
                if not (self._ser and self._ser.is_open):
                    break
                # Blocking read — the port's own 1 s timeout paces this loop.
                # The previous version polled in_waiting with no sleep at all,
                # so it burned a whole core and held the GIL for the entire
                # 120 s measurement, starving the GUI and the SpO2 sampler.
                raw = self._ser.readline()
            except serial.SerialException as e:
                if self._running:
                    print(f"[BPMonitor] Serial error: {e}")
                break
            except Exception as e:
                if not self._running:
                    break
                print(f"[BPMonitor] Error: {e}")
                time.sleep(0.05)
                continue

            if not raw:
                # Read timed out. Anything still buffered was a line the device
                # sent without a trailing terminator — take it as complete now.
                if self._partial:
                    self._flush_partial()
                else:
                    time.sleep(0.01)
                continue

            # Split on CR, LF, or CRLF alike. readline() only breaks on LF, so
            # firmware that ends its lines with a bare CR would otherwise pile
            # up in _partial and never be handled at all while it kept talking.
            buf = (self._partial + raw).replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            parts = buf.split(b"\n")
            self._partial = parts.pop()  # trailing fragment, terminator not seen yet

            for part in parts:
                line = part.decode(errors="ignore").strip()
                if line:
                    self._handle_line(line)

    def _flush_partial(self):
        line = self._partial.decode(errors="ignore").strip()
        self._partial = b""
        if line:
            self._handle_line(line)

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
                # The reading is already valid here; unblock measure() now
                # instead of waiting for the later "READY" line, so the value
                # reaches the screen without the firmware's SYS->READY delay.
                self._done_event.set()

        # ── Error: "BP_ERROR" ──
        elif line == "BP_ERROR":
            self._last_error = self.ERR_DEVICE
            print("\n[BPMonitor] Measurement Error — please wait 2 min\n")
            if self.on_error:
                self.on_error("BP_ERROR")
            # The device has already given its verdict, so stop waiting. This
            # used to fall through and let measure() burn its whole 120 s
            # timeout on a failure that was known within ~20 s.
            self._done_event.set()

        elif line == "READY":
            self._is_ready = True
            self._ready_event.set()
            self._done_event.set()
            print("[BPMonitor] READY !! ")
            if self.on_ready:
                self.on_ready()

        elif line.startswith("NOT_READY"):
            # Firmware answers "NOT_READY:MEASURING" / "NOT_READY:WAIT_SHUTDOWN"
            # so the caller can tell "the previous run is still going" apart
            # from "the module is powering down" -- the waits are different.
            _, _, state = line.partition(":")
            self.busy_state = state.strip() or None
            self._is_ready = False
            self._last_error = self.ERR_NOT_READY
            print(f"[BPMonitor] NOT_READY (firmware state: {self.busy_state or 'unknown'})")
            self._done_event.set()

        else:
            # Anything not recognised -- boot chatter, firmware banners, lines
            # nobody documented. Dropping these in silence is what made "the
            # bridge said nothing" and "the bridge said something we do not
            # parse" impossible to tell apart from the logs.
            print(f"[BPMonitor] <- {line!r}")

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

def main_raw(port: str, seconds: float = 90.0, send_start: bool = True):
    """Dump every byte the bridge sends, with timestamps. Diagnostic only.

    Answers the questions the parsed path cannot: does opening the port reboot
    the board (boot chatter appears), does the firmware announce itself, does
    START get a reply at all, and what do its line endings actually look like.
    Nothing is parsed or interpreted here on purpose."""
    print(f"[raw] opening {port} ...")
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, 115200, 0.2
    ser.open()
    opened = time.time()

    def stamp():
        return f"{time.time() - opened:6.2f}s"

    try:
        if send_start:
            print(f"[raw] {stamp()} listening 3s before START (boot chatter?)")
            deadline = time.time() + 3
            while time.time() < deadline:
                chunk = ser.read(256)
                if chunk:
                    print(f"[raw] {stamp()} <- {chunk!r}")
            ser.write(b"START\n")
            print(f"[raw] {stamp()} -> b'START\\n'")

        deadline = time.time() + seconds
        while time.time() < deadline:
            chunk = ser.read(256)
            if chunk:
                print(f"[raw] {stamp()} <- {chunk!r}")
        print(f"[raw] {stamp()} done")
    finally:
        ser.close()


if __name__ == "__main__":
    import sys

    port = "/dev/ttyUSB0"  # Windows: "COM3" / Linux: "/dev/ttyUSB0"
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            port = arg

    if "--raw" in sys.argv:
        main_raw(port, send_start="--listen" not in sys.argv)
    else:
        bp = BPMonitor(port=port)
        bp.connect()
        started = time.time()
        result = bp.measure()
        elapsed = time.time() - started

        if result:
            print(f"result: {result}  ({elapsed:.1f}s)")
        else:
            print(f"BP Measurement fail ({bp.last_error}) after {elapsed:.1f}s")

        bp.disconnect()
