# -*- coding: utf-8 -*-
from __future__ import annotations

import getpass
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRectF, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QBrush, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QDialogButtonBox, 
)

from carekeeper_style import build_stylesheet
from carekeeper_providers import (
    BloodPressureReading,
    CareKeeperProvider,
    DeviceStatus,
    MeasurementHistoryRecord,
    PatientInfo,
)
from carekeeper_retry import SubsystemRegistry
from carekeeper_queue import QueueDrainWorker, SubmissionQueue
from carekeeper_logging import log_thread_identity

WINDOW_WIDTH = 1010
WINDOW_HEIGHT = 503
PROJECT_DIR = Path(__file__).resolve().parent
STYLE_DIR = PROJECT_DIR / "style"
APP_FONT_FAMILY = "Noto Sans Thai"
NUMBER_FONT_FAMILY = "Asimov-MwEn"

# Exit status the "close display" button uses. systemd (see conf/carekeeper.service)
# is told to auto-restart the kiosk on every exit EXCEPT this one, via
# RestartPreventExitStatus -- so a deliberate close drops to the desktop and
# stays there, while a real crash and a reboot still bring the GUI back.
EXIT_CODE_CLOSE_DISPLAY = 42

# Shared look for the system pop-up dialogs (power menu, device ID, exit).
# These are modal QMessageBoxes, so they never touch the main window layout.
_SYSTEM_DIALOG_STYLE = """
    QMessageBox { background-color: #ffffff; }
    QLabel { font-size: 20px; font-weight: 700; color: #0b1f33; }
    QPushButton {
        font-size: 18px;
        font-weight: 800;
        min-height: 48px;
        min-width: 138px;
        border-radius: 12px;
        background-color: #eef2f6;
        color: #0b1f33;
        border: 2px solid #9ec9d6;
        padding: 4px 10px;
    }
    QPushButton:hover { background-color: #dbe7ee; }
"""

def _style_asset(name: str) -> Path:
    return STYLE_DIR / name

def _load_font_family(font_path: Path, fallback: str) -> str:
    if not font_path.exists():
        return fallback

    font_id = QFontDatabase.addApplicationFont(str(font_path))
    if font_id == -1:
        return fallback

    families = QFontDatabase.applicationFontFamilies(font_id)
    return families[0] if families else fallback

def _load_app_font(app: QApplication) -> str:
    family = APP_FONT_FAMILY

    font_candidates = (
        STYLE_DIR / "IBMPlexSansThai-Regular.ttf",
        PROJECT_DIR / "IBMPlexSansThai-Regular.ttf",
        STYLE_DIR / "NotoSansThai-Regular.ttf",
        PROJECT_DIR / "NotoSansThai-Regular.ttf",
    )
    for font_path in font_candidates:
        if not font_path.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id != -1:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                family = families[0]
                break

    app.setFont(QFont(family, 12))
    return family

def _load_number_font() -> str:
    return _load_font_family(STYLE_DIR / "Asimov-MwEn.otf", NUMBER_FONT_FAMILY)

def _tinted_icon(name: str, size: int, color: str = "#ffffff") -> QPixmap:
    source = QPixmap(str(_style_asset(name)))
    if source.isNull():
        return source

    icon = source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    tinted = QPixmap(icon.size())
    tinted.fill(Qt.transparent)

    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, icon)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), QColor(color))
    painter.end()
    return tinted

@dataclass
class VitalState:
    systolic: int | None = None
    diastolic: int | None = None
    pulse: int | None = None
    spo2: int | None = None
    temperature: float | None = None

class ProviderTask(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, action: Callable[[], object], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.action = action

    def run(self) -> None:
        log_thread_identity(f"ProviderTask:{getattr(self.action, '__name__', self.action)!r}")
        try:
            self.completed.emit(self.action())
        except Exception as exc:
            self.failed.emit(str(exc))


class RetryNotifier(QObject):
    """Bridges retry-attempt/give-up callbacks (fired on worker threads) back
    to the GUI thread via Qt's standard cross-thread signal delivery."""

    attempt = Signal(str, int, int)
    give_up = Signal(str, str)


class QueueDrainNotifier(QObject):
    """Bridges the offline-queue background worker's outcomes back to the
    GUI thread, same cross-thread-signal pattern as RetryNotifier."""

    drain_success = Signal(int)
    drain_failure = Signal(int, str)


class MeasurementProgressNotifier(QObject):
    """Moves live sensor readings from provider threads onto the GUI thread."""

    progress = Signal(str, object, object)

class WifiIndicator(QWidget):
    clicked = Signal()
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.connected = False
        self.scale = 1.35
        self.setFixedSize(int(26 * self.scale), int(20 * self.scale))
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(self.scale, self.scale)

        color = QColor("#75efff") if self.connected else QColor("#7c92a4")
        cx = 26 / 2  # ล็อกพิกัดเดิมไว้ไม่ให้เพี้ยน
        cy = 20 - 3

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawEllipse(cx - 1.5, cy - 1.5, 3, 3)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, 2.0, Qt.SolidLine, Qt.RoundCap))
        for radius, span in ((4, 75), (8, 75), (12, 75)):
            start_angle = int((90 - span / 2) * 16)
            painter.drawArc(cx - radius, cy - radius, radius * 2, radius * 2, start_angle, span * 16)

class BluetoothIndicator(QWidget):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.connected = False
        self.scale = 1.45
        self.setFixedSize(int(20 * self.scale), int(20 * self.scale))
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(self.scale, self.scale)
        color = QColor("#75efff") if self.connected else QColor("#7c92a4")
        painter.setPen(QPen(color, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawLine(10, 3, 10, 17)
        painter.drawLine(10, 3, 15, 7)
        painter.drawLine(15, 7, 5, 14)
        painter.drawLine(10, 17, 15, 13)
        painter.drawLine(15, 13, 5, 6)

class BatteryIndicator(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.percent = 0
        self.scale = 1.15
        self.setFixedSize(int(30 * self.scale), int(15 * self.scale))

    def set_percent(self, percent: int | None) -> None:
        self.percent = 0 if percent is None else max(0, min(100, percent))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(self.scale, self.scale)
        border = QColor("#16324f")
        fill = QColor("#75efff") if self.percent > 20 else QColor("#7c92a4")
        painter.setPen(QPen(border, 1.2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(1, 1, 24, 13, 3, 3)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(border))
        painter.drawRoundedRect(26, 4, 2, 6, 1, 1)

        fill_width = int((self.percent / 100) * 20)
        if fill_width:
            painter.setBrush(QBrush(fill))
            painter.drawRoundedRect(3, 3, fill_width, 9, 1.5, 1.5)

class PowerButton(QWidget):
    """Hand-drawn power icon (avoids relying on a font glyph that may render as a box)."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(56, 56)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("เมนูระบบ — ดู ID/IP, ปิดหน้าจอ, รีสตาร์ท, ปิดเครื่อง")
        self.hovered = False

    def enterEvent(self, event) -> None:
        self.hovered = True
        self.update()

    def leaveEvent(self, event) -> None:
        self.hovered = False
        self.update()

    def mousePressEvent(self, event) -> None:
        if self.isEnabled() and event.button() == Qt.LeftButton:
            self.clicked.emit()

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if not self.isEnabled():
            bg, border, icon = QColor("#f1f5f9"), QColor("#cbd5e1"), QColor("#94a3b8")
        elif self.hovered:
            bg, border, icon = QColor("#fee2e2"), QColor("#fca5a5"), QColor("#b91c1c")
        else:
            bg, border, icon = QColor("#ffffff"), QColor("#9ec9d6"), QColor("#475569")

        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(border, 2))
        painter.setBrush(QBrush(bg))
        painter.drawEllipse(rect)

        cx, cy = rect.center().x(), rect.center().y()
        radius = rect.width() * 0.22
        painter.setPen(QPen(icon, 2.6, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        arc_rect = QRectF(cx - radius, cy - radius + 1, radius * 2, radius * 2)
        start_angle = int((90 + 35) * 16)
        span_angle = int((360 - 70) * 16)
        painter.drawArc(arc_rect, start_angle, span_angle)
        painter.drawLine(int(cx), int(cy - radius - 3), int(cx), int(cy - 1))

class ToastLabel(QLabel):
    def mousePressEvent(self, event) -> None:
        self.hide()


class ElidedLabel(QLabel):
    """QLabel that truncates its text with a trailing ellipsis when the layout
    gives it less width than the text needs, instead of forcing the row to
    overflow. The patient header used plain QLabels, so a long name pushed past
    the citizen-ID and the two collided on the Pi's narrow fullscreen. Keeps the
    full text available via text() so callers still read the real value."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = text

    def setText(self, text: str) -> None:
        self._full_text = text
        self._elide_to_width()
        self.updateGeometry()

    def text(self) -> str:
        return self._full_text

    def sizeHint(self):
        # Base the preferred width on the FULL text, never the currently-elided
        # text. Otherwise an elision that happens while the page is hidden (width
        # ~0) shrinks the hint and the label stays collapsed even after its page
        # is shown with plenty of room.
        hint = super().sizeHint()
        hint.setWidth(self.fontMetrics().horizontalAdvance(self._full_text) + 2)
        return hint

    def minimumSizeHint(self):
        # Let the layout shrink us below the natural text width; we elide to
        # whatever width we actually receive.
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide_to_width()

    def _elide_to_width(self) -> None:
        elided = self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, max(0, self.width()))
        super().setText(elided)


class PopupOverlay(QWidget):
    """Full-window dimmed overlay with a centered message card (used for important confirmations)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color: rgba(11, 31, 51, 165);")
        self.setCursor(Qt.PointingHandCursor)

        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)

        self.card = QFrame(self)
        self.card.setObjectName("PopupCard")
        self.card.setMinimumWidth(420)
        self.card.setMaximumWidth(620)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(48, 40, 48, 40)
        card_layout.setSpacing(16)
        card_layout.setAlignment(Qt.AlignCenter)

        self.icon_label = QLabel(self.card)
        self.icon_label.setAlignment(Qt.AlignCenter)

        self.message_label = QLabel(self.card)
        self.message_label.setAlignment(Qt.AlignCenter)
        self.message_label.setWordWrap(True)

        card_layout.addWidget(self.icon_label)
        card_layout.addWidget(self.message_label)
        outer.addWidget(self.card)

        self.hide()

    def show_message(self, message: str, success: bool = True) -> None:
        accent = "#0ea672" if success else "#dc2626"
        self.icon_label.setText("✓" if success else "✕")
        self.icon_label.setStyleSheet(
            f"font-size:64px; font-weight:900; color:{accent}; background:transparent;"
        )
        self.message_label.setText(message)
        self.message_label.setStyleSheet(
            "font-size:24px; font-weight:800; color:#0b1f33; background:transparent;"
        )
        self.card.setStyleSheet(
            f"QFrame#PopupCard {{ background:#ffffff; border-radius:24px; border:3px solid {accent}; }}"
        )

    def mousePressEvent(self, event) -> None:
        self.hide()


class LiveNumberDisplay(QObject):
    """Animates a value while measuring, then settles on the final reading."""

    def __init__(self, label: QLabel, decimals: int, parent: QObject) -> None:
        super().__init__(parent)
        self.label = label
        self.decimals = decimals
        self.current: float | None = None
        self.target: float | None = None
        self.busy_values: tuple[float, ...] = ()
        self.busy_index = 0
        self.timer = QTimer(self)
        self.timer.setInterval(70)
        self.timer.timeout.connect(self._tick)

    def begin_busy(self, values: tuple[float, ...]) -> None:
        self.timer.stop()
        self.current = None
        self.target = None
        self.busy_values = values
        self.busy_index = 0
        self.label.setText("--")
        self.timer.setInterval(140)
        self.timer.start()

    def set_target(self, value: float) -> None:
        self.busy_values = ()
        self.target = float(value)
        self.timer.setInterval(70)
        if self.current is None:
            self.current = self.target
            self._render()
            return
        if not self.timer.isActive():
            self.timer.start()

    def finish(self, value: float) -> None:
        self.timer.stop()
        self.busy_values = ()
        self.current = float(value)
        self.target = self.current
        self._render()

    def stop(self) -> None:
        self.timer.stop()
        self.busy_values = ()
        self.current = None
        self.target = None

    def _tick(self) -> None:
        if self.busy_values:
            self.current = self.busy_values[self.busy_index]
            self.busy_index = (self.busy_index + 1) % len(self.busy_values)
            self._render()
            return

        if self.current is None or self.target is None:
            self.timer.stop()
            return

        difference = self.target - self.current
        minimum_step = 1.0 if self.decimals == 0 else 0.1
        if abs(difference) <= minimum_step:
            self.current = self.target
            self.timer.stop()
        else:
            self.current += minimum_step if difference > 0 else -minimum_step
        self._render()

    def _render(self) -> None:
        if self.current is None:
            self.label.setText("--")
        elif self.decimals == 0:
            self.label.setText(str(int(round(self.current))))
        else:
            self.label.setText(f"{self.current:.{self.decimals}f}")

_SUBSYSTEM_LABELS = {
    "wifi": "Wi-Fi",
    "bluetooth": "Bluetooth",
    "bp_monitor": "เครื่องวัดความดัน",
    "spo2": "เครื่องวัดออกซิเจน",
    "idcard": "เครื่องอ่านบัตร",
}


class CareKeeperWindow(QMainWindow):
    # How long the NIBP button stays locked after a measurement. A good reading
    # only needs the cuff to deflate and the arm to recover; a device-reported
    # BP_ERROR also parks the cuff in a lockout of its own.
    #
    # The error cooldown is deliberately SHORTER than that lockout (the
    # firmware asks for ~2 min). Holding the operator for the full two minutes
    # costs more than an early retry does: a retry that lands too soon now
    # comes straight back as NOT_READY with its own message in well under a
    # second, instead of hanging, so letting them try again at 60 s is cheap.
    BP_COOLDOWN_AFTER_SUCCESS = 60
    BP_COOLDOWN_AFTER_DEVICE_ERROR = 60

    def __init__(self, provider: CareKeeperProvider, mode_name: str = "Mock") -> None:
        super().__init__()
        self.provider = provider
        self.mode_name = mode_name
        self.patient = PatientInfo()
        self.vitals = VitalState()
        self.tasks: list[ProviderTask] = []
        self.status_task: ProviderTask | None = None
        self.network_task: ProviderTask | None = None
        self.bp_cooldown_seconds = 0
        self.bp_cooldown_succeeded = True
        self.status_fail_count = 0
        self.last_status_warning_ts = 0.0
        self.last_queue_warning_ts = 0.0

        self.retry_notifier = RetryNotifier()
        self.retry_notifier.attempt.connect(self._on_retry_attempt)
        self.retry_notifier.give_up.connect(self._on_retry_giveup)
        self.provider.on_retry_attempt = lambda s, a, m: self.retry_notifier.attempt.emit(s, a, m)
        self.provider.on_retry_giveup = lambda s, r: self.retry_notifier.give_up.emit(s, r)

        self.measurement_notifier = MeasurementProgressNotifier()
        self.measurement_notifier.progress.connect(self._on_measurement_progress)
        self.provider.on_measurement_progress = (
            lambda kind, value, state: self.measurement_notifier.progress.emit(kind, value, state)
        )
        self.measurement_active = {"bp": False, "spo2": False, "temp": False}

        self.submission_queue = SubmissionQueue()
        self.queue_notifier = QueueDrainNotifier()
        self.queue_notifier.drain_success.connect(self._on_queue_drain_success)
        self.queue_notifier.drain_failure.connect(self._on_queue_drain_failure)
        self.queue_worker = QueueDrainWorker(
            queue=self.submission_queue,
            send_fn=self.provider.send_data,
            is_online_fn=getattr(self.provider, "_is_wifi_connected", lambda: True),
            on_drain_success=lambda row_id: self.queue_notifier.drain_success.emit(row_id),
            on_drain_failure=lambda row_id, err: self.queue_notifier.drain_failure.emit(row_id, err),
        )
        self.queue_worker.start()

        self.setWindowTitle(f"CareKeeper - {mode_name}")
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setMinimumSize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self._build_scan_page()
        self._build_dashboard_page()
        self._build_summary_page()
        self._apply_styles()
        self._build_toast()
        self._build_live_value_displays()
        self._refresh_patient()
        self._refresh_values()

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._request_device_status)
        self.status_timer.start(6000)
        self._request_device_status()

        self.cooldown_timer = QTimer(self)
        self.cooldown_timer.timeout.connect(self._bp_cooldown_tick)

    def closeEvent(self, event) -> None:
        self.queue_worker.stop()
        super().closeEvent(event)

    def _build_toast(self) -> None:
        self._toast_token = 0
        self.toast = ToastLabel(self)
        self.toast.setAlignment(Qt.AlignCenter)
        self.toast.setWordWrap(True)
        self.toast.setCursor(Qt.PointingHandCursor)
        self.toast.hide()

        self.popup_overlay = PopupOverlay(self)

    def _build_live_value_displays(self) -> None:
        self.bp_sys_live_display = LiveNumberDisplay(self.lbl_sys_value, 0, self)
        self.bp_dia_live_display = LiveNumberDisplay(self.lbl_dia_value, 0, self)
        self.bp_pulse_live_display = LiveNumberDisplay(self.lbl_pulse_value, 0, self)
        self.spo2_live_display = LiveNumberDisplay(self.lbl_spo2_value, 0, self)
        self.temp_live_display = LiveNumberDisplay(self.lbl_temp_value, 1, self)

    def _begin_measurement_display(self, kind: str) -> None:
        self._clear_measurement_value(kind)
        self.measurement_active[kind] = True
        if kind == "bp":
            self.bp_sys_live_display.begin_busy((88, 104, 121, 139, 158, 142, 126, 116))
            self.bp_dia_live_display.begin_busy((54, 62, 71, 83, 92, 86, 78, 72))
            self.bp_pulse_live_display.begin_busy((64, 68, 73, 77, 81, 75, 70, 68))
        elif kind == "spo2":
            self.spo2_live_display.begin_busy((91, 93, 95, 97, 98, 96))
        elif kind == "temp":
            self.temp_live_display.begin_busy((35.4, 35.8, 36.1, 36.4, 36.7, 36.5))

    def _clear_measurement_value(self, kind: str) -> None:
        if kind == "bp":
            self.vitals.systolic = None
            self.vitals.diastolic = None
            self.vitals.pulse = None
            self.lbl_sys_value.setText("--")
            self.lbl_dia_value.setText("--")
            self.lbl_pulse_value.setText("--")
            self.sum_bp_value.setText("--/--")
            self.sum_pulse_value.setText("--")
        elif kind == "spo2":
            self.vitals.spo2 = None
            self.lbl_spo2_value.setText("--")
            self.sum_spo2_value.setText("--")
        elif kind == "temp":
            self.vitals.temperature = None
            self.lbl_temp_value.setText("--")
            self.sum_temp_value.setText("--")
        self._refresh_summary_badges()
        self._refresh_summary_button()

    def _finish_measurement_display(self, kind: str, value: object | None = None) -> None:
        self.measurement_active[kind] = False
        if kind == "bp":
            self.bp_sys_live_display.stop()
            self.bp_dia_live_display.stop()
            self.bp_pulse_live_display.stop()
        elif kind == "spo2" and value is not None:
            self.spo2_live_display.finish(float(value))
        elif kind == "temp" and value is not None:
            self.temp_live_display.finish(float(value))

    def _cancel_measurement_display(self, kind: str) -> None:
        self.measurement_active[kind] = False
        if kind == "bp":
            self.bp_sys_live_display.stop()
            self.bp_dia_live_display.stop()
            self.bp_pulse_live_display.stop()
            self.lbl_sys_value.setText("--")
            self.lbl_dia_value.setText("--")
            self.lbl_pulse_value.setText("--")
        elif kind == "spo2":
            self.spo2_live_display.stop()
            self.lbl_spo2_value.setText("--")
        elif kind == "temp":
            self.temp_live_display.stop()
            self.lbl_temp_value.setText("--")

    def _on_measurement_progress(self, kind: str, value: object, state: object) -> None:
        if kind not in self.measurement_active or not self.measurement_active[kind]:
            return

        details = state if isinstance(state, dict) else {}
        if kind == "spo2":
            if not details.get("finger_detected", True):
                self.spo2_live_display.begin_busy((91, 93, 95, 97, 98, 96))
                self._set_system_message("กรุณาวางนิ้วให้แนบเต็มเซนเซอร์", success=None)
            elif value is None:
                self._set_system_message("กำลังอ่านสัญญาณ กรุณาวางนิ่งไว้", success=None)
            else:
                self.spo2_live_display.set_target(float(value))
                self._set_system_message("ค่ากำลังเปลี่ยน กรุณารอจนกว่าจะนิ่ง", success=None)
        elif kind == "temp":
            if not details.get("in_contact", True):
                self.temp_live_display.begin_busy((35.4, 35.8, 36.1, 36.4, 36.7, 36.5))
                self._set_system_message("กรุณาแนบเซนเซอร์กับผิวให้สนิท", success=None)
            elif value is not None:
                self.temp_live_display.set_target(float(value))
                self._set_system_message("ค่ากำลังเปลี่ยน กรุณารอจนกว่าจะนิ่ง", success=None)

    def _show_popup(self, message: str, success: bool = True, duration_ms: int = 2200) -> None:
        self.popup_overlay.show_message(message, success=success)
        self.popup_overlay.setGeometry(self.rect())
        self.popup_overlay.raise_()
        self.popup_overlay.show()
        QTimer.singleShot(duration_ms, self.popup_overlay.hide)

    def _show_toast(self, message: str, success: bool = True, duration_ms: int = 2000) -> None:
        self._toast_token += 1
        token = self._toast_token
        background = "#f0fdf4" if success else "#fff1f2"
        color = "#064e3b" if success else "#7f1d1d"
        border = "#22c55e" if success else "#fb7185"
        self.toast.setText(message)
        self.toast.setStyleSheet(
            f"background:{background}; color:{color}; border:3px solid {border}; "
            "border-radius:8px; padding:14px 20px; font-size:18px; font-weight:900;"
        )
        width = min(500, max(360, self.width() - 260))
        height = 92 if len(message) <= 48 else 118
        self.toast.setGeometry((self.width() - width) // 2, (self.height() - height) // 2, width, height)
        self.toast.raise_()
        self.toast.show()
        QTimer.singleShot(duration_ms, lambda: self.toast.hide() if token == self._toast_token else None)

    def _start_task(
        self,
        action: Callable[[], object],
        on_success: Callable[[object], None],
        on_failed: Callable[[str], None],
    ) -> None:
        task = ProviderTask(action, self)
        task.completed.connect(on_success)
        task.failed.connect(on_failed)
        task.finished.connect(lambda: self._release_task(task))
        self.tasks.append(task)
        task.start()

    def _release_task(self, task: ProviderTask) -> None:
        if task in self.tasks:
            self.tasks.remove(task)
        if self.status_task is task:
            self.status_task = None
        if self.network_task is task:
            self.network_task = None
        task.deleteLater()

    def _start_network_task(
        self,
        action: Callable[[], object],
        on_success: Callable[[object], None],
        on_failed: Callable[[str], None],
    ) -> bool:
        if self.network_task and self.network_task.isRunning():
            self._show_toast("กำลังดำเนินการเชื่อมต่ออยู่ กรุณารอสักครู่", success=False, duration_ms=1800)
            return False

        task = ProviderTask(action, self)
        self.network_task = task
        task.completed.connect(on_success)
        task.failed.connect(on_failed)
        task.finished.connect(lambda: self._release_task(task))
        self.tasks.append(task)
        task.start()
        return True

    def _request_device_status(self) -> None:
        if self.status_task and self.status_task.isRunning():
            return
        task = ProviderTask(self.provider.get_device_status, self)
        self.status_task = task
        task.completed.connect(self._on_status_done)
        task.failed.connect(self._on_status_failed)
        task.finished.connect(lambda: self._release_task(task))
        task.start()
    def _build_numeric_keypad(self, target: QLineEdit) -> QFrame:
        keypad = QFrame()
        keypad.setObjectName("NumericKeypad")
        keypad.setStyleSheet("QFrame#NumericKeypad { background: transparent; }")

        grid = QGridLayout(keypad)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        keys = [
            ["1", "2", "3"],
            ["4", "5", "6"],
            ["7", "8", "9"],
            ["C", "0", "<-"],
        ]

        for row, key_row in enumerate(keys):
            for col, key in enumerate(key_row):
                btn = QPushButton(key)
                btn.setFixedSize(126, 50)
                btn.setCursor(Qt.PointingHandCursor)

                btn.setStyleSheet(
                    """
                    QPushButton {
                        background-color: #101418;
                        color: #9aff2d;
                        border: 2px solid #2a343d;
                        border-radius: 14px;
                        font-size: 28px;
                        font-weight: 900;
                    }
                    QPushButton:pressed {
                        background-color: #1f2937;
                        border: 2px solid #9aff2d;
                        color: #ffffff;
                    }
                    """
                )

                btn.clicked.connect(lambda checked=False, value=key: self._numeric_key_pressed(target, value))
                grid.addWidget(btn, row, col)

        return keypad


    def _numeric_key_pressed(self, target: QLineEdit, key: str) -> None:
        if key == "C":
            target.clear()
            return

        if key == "<-":
            if target is getattr(self, "txt_manual_cid", None):
                digits = "".join(ch for ch in target.text() if ch.isdigit())
                target.setText(digits[:-1])
            else:
                target.backspace()
            return

        if not key.isdigit():
            return

        if target is getattr(self, "txt_manual_cid", None):
            digits = "".join(ch for ch in target.text() if ch.isdigit())
            if len(digits) >= 13:
                return
            target.setText(digits + key)
        else:
            target.insert(key)

    def _styled_input_dialog(self, title: str) -> QInputDialog:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setWindowFlag(Qt.FramelessWindowHint, True)
        dialog.setMinimumSize(620, 460)
        dialog.setStyleSheet(
            """
            QInputDialog {
                background-color: #050709;
                color: #f8fafc;
            }

            QLabel {
                font-size: 22px;
                font-weight: 900;
                color: #f8fafc;
                background: transparent;
            }

            QComboBox {
                font-size: 22px;
                min-height: 56px;
                padding: 6px 14px;
                border: 2px solid #2a343d;
                border-radius: 12px;
                color: #f8fafc;
                background-color: #101418;
            }

            QComboBox QAbstractItemView {
                font-size: 22px;
                min-height: 50px;
                color: #f8fafc;
                background-color: #101418;
                border: 2px solid #2a343d;
                selection-background-color: #1f2937;
                selection-color: #9aff2d;
            }

            QInputDialog QLineEdit {
                font-size: 22px;
                min-height: 56px;
                padding: 6px 14px;
                border: 2px solid #2a343d;
                border-radius: 12px;
                color: #f8fafc;
                background-color: #101418;
                selection-background-color: #9aff2d;
                selection-color: #050709;
            }

            QPushButton {
                font-size: 20px;
                font-weight: 900;
                min-height: 52px;
                min-width: 130px;
                border-radius: 12px;
                background-color: #101418;
                color: #9aff2d;
                border: 2px solid #2a343d;
            }

            QPushButton:pressed {
                background-color: #1f2937;
                border: 2px solid #9aff2d;
                color: #ffffff;
            }
            """
        )
        return dialog

    def _select_from_list(self, title: str, label: str, items: list[str]) -> tuple[str, bool]:
        dialog = self._styled_input_dialog(title)
        dialog.setComboBoxItems(items)
        dialog.setLabelText(label)
        dialog.setComboBoxEditable(False)

        button_box = dialog.findChild(QDialogButtonBox)
        if button_box:
            button_box.setLayoutDirection(Qt.RightToLeft)

            ok_button = button_box.button(QDialogButtonBox.Ok)
            cancel_button = button_box.button(QDialogButtonBox.Cancel)

            if ok_button:
                ok_button.setText("ยืนยัน")
            if cancel_button:
                cancel_button.setText("ยกเลิก")

        ok = bool(dialog.exec())
        return dialog.textValue(), ok

    def _ask_password(self, title: str, label: str) -> tuple[str, bool]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setWindowFlag(Qt.FramelessWindowHint, True)
        dialog.setModal(True)
        dialog.setFixedSize(620, 470)
        dialog.setStyleSheet(
            """
            QDialog {
                background-color: #050709;
                color: #f8fafc;
            }

            QLabel {
                font-size: 20px;
                font-weight: 900;
                color: #f8fafc;
                background: transparent;
            }

            QLineEdit {
                font-size: 26px;
                min-height: 56px;
                padding: 6px 14px;
                border: 2px solid #2a343d;
                border-radius: 12px;
                color: #9aff2d;
                background-color: #101418;
                selection-background-color: #9aff2d;
                selection-color: #050709;
            }

            QPushButton#BtnConfirmManualCid {
                background-color: #0b7cff;
                color: #ffffff;
                border: none;
                border-radius: 12px;
                font-size: 20px;
                font-weight: 900;
            }

            QPushButton#BtnConfirmManualCid:pressed {
                background-color: #0b7476;
            }

            QPushButton#BtnCancelManualCid {
                background-color: #101418;
                color: #f8fafc;
                border: 2px solid #2a343d;
                border-radius: 12px;
                font-size: 20px;
                font-weight: 900;
            }

            QPushButton#BtnCancelManualCid:pressed {
                background-color: #1f2937;
                border: 2px solid #9aff2d;
            }
            """
        )

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(10)

        title_label = QLabel(label)
        title_label.setAlignment(Qt.AlignCenter)

        txt_password = QLineEdit()
        # txt_password.setEchoMode(QLineEdit.Password)
        txt_password.setFocusPolicy(Qt.StrongFocus)
        txt_password.setAlignment(Qt.AlignCenter)
        txt_password.setFixedHeight(56)

        keypad = self._build_numeric_keypad(txt_password)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        actions.addStretch(1)

        btn_cancel = QPushButton("ยกเลิก")
        btn_cancel.setObjectName("BtnCancelManualCid")
        btn_cancel.setFixedSize(150, 48)
        btn_cancel.clicked.connect(dialog.reject)

        btn_ok = QPushButton("ยืนยัน")
        btn_ok.setObjectName("BtnConfirmManualCid")
        btn_ok.setFixedSize(150, 48)
        btn_ok.clicked.connect(dialog.accept)

        actions.addWidget(btn_cancel)
        actions.addWidget(btn_ok)
        actions.addStretch(1)

        layout.addWidget(title_label)
        layout.addWidget(txt_password)
        layout.addWidget(keypad, alignment=Qt.AlignCenter)
        layout.addSpacing(4)
        layout.addLayout(actions)

        txt_password.setFocus(Qt.OtherFocusReason)

        ok = bool(dialog.exec())
        return txt_password.text(), ok

    def _open_power_menu(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("เมนูระบบ")
        box.setText("ต้องการดำเนินการใด?")
        box.setIcon(QMessageBox.Question)
        btn_exit_ui = box.addButton("ปิดหน้าจอ", QMessageBox.ActionRole)
        btn_reboot = box.addButton("รีสตาร์ทเครื่อง", QMessageBox.ActionRole)
        btn_shutdown = box.addButton("ปิดเครื่อง", QMessageBox.DestructiveRole)
        btn_view_id = box.addButton("ดู IP ", QMessageBox.ActionRole)
        btn_cancel = box.addButton("ยกเลิก", QMessageBox.RejectRole)
        box.setDefaultButton(btn_cancel)
        box.setStyleSheet(_SYSTEM_DIALOG_STYLE)
        box.exec()
        clicked = box.clickedButton()
        if clicked == btn_reboot:
            self._do_reboot()
        elif clicked == btn_shutdown:
            self._do_shutdown()
        elif clicked == btn_view_id:
            self._show_device_id()
        elif clicked == btn_exit_ui:
            self._exit_ui()

    def _show_device_id(self) -> None:
        """Show the device's current network identity (IP / hostname / MAC) so
        the operator can SSH/VNC in after the network changes -- the on-screen
        answer to "what's my IP now?" without needing to already be remoted in."""
        try:
            ip = self.provider.get_ip_address()
        except Exception:
            ip = ""
        hostname = socket.gethostname()
        mac = getattr(self.provider, "device_mac", "") or "-"
        user = getpass.getuser()

        if ip:
            primary = ip.split()[0]
            body = (
                f"IP:  {ip}\n"
                f"Hostname:  {hostname}\n"
                f"MAC:  {mac}\n\n"
                f"SSH:  ssh {user}@{primary}"
            )
        else:
            body = (
                "ยังไม่พบ IP — เครื่องอาจยังไม่ได้เชื่อมต่อเครือข่าย\n"
                f"Hostname:  {hostname}\n"
                f"MAC:  {mac}"
            )

        box = QMessageBox(self)
        box.setWindowTitle("ข้อมูลเครื่อง (Device ID)")
        box.setText(body)
        box.setIcon(QMessageBox.Information)
        # Must add a button explicitly: a button-less QMessageBox has no way to
        # close in the frameless full-screen kiosk, trapping the operator.
        box.addButton("ปิด", QMessageBox.AcceptRole)
        # Let the operator select/copy the IP or ssh line if a keyboard is attached.
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.setStyleSheet(_SYSTEM_DIALOG_STYLE)
        box.exec()

    def _exit_ui(self) -> None:
        """Close the kiosk UI back to the desktop without powering off, so the
        Pi can be configured locally when remote access is unavailable.

        systemd keeps the kiosk alive (Restart=on-failure), so the exit has to
        look deliberate or the service just relaunches a few seconds later. We
        exit with EXIT_CODE_CLOSE_DISPLAY, which conf/carekeeper.service
        whitelists via RestartPreventExitStatus -- systemd then leaves the app
        down until the next reboot or a manual start, while real crashes still
        auto-recover."""
        confirm = QMessageBox(self)
        confirm.setWindowTitle("ปิดหน้าจอ")
        confirm.setText(
            "ออกจากหน้าจอแอปไปที่หน้าหลัก?\n"
            "ต้องปิดเเละเปิดเครื่องเพื่อเปิดแอปกลับ"
        )
        confirm.setIcon(QMessageBox.Warning)
        btn_yes = confirm.addButton("ปิดหน้าจอ", QMessageBox.AcceptRole)
        btn_no = confirm.addButton("ยกเลิก", QMessageBox.RejectRole)
        confirm.setDefaultButton(btn_no)
        confirm.setStyleSheet(_SYSTEM_DIALOG_STYLE)
        confirm.exec()
        if confirm.clickedButton() == btn_yes:
            # Hard exit, not QApplication.quit(): a normal Qt teardown while a
            # background QThread (e.g. the periodic status poll) is still running
            # aborts with a non-zero status, which systemd's on-failure would
            # relaunch -- the "closes then reappears" bug. os._exit delivers
            # exactly EXIT_CODE_CLOSE_DISPLAY every time, so the whitelist holds.
            os._exit(EXIT_CODE_CLOSE_DISPLAY)

    def _do_reboot(self) -> None:
        self.btn_power.setEnabled(False)
        self._show_toast("กำลังรีสตาร์ทเครื่อง...", success=True, duration_ms=4000)
        self._start_task(self.provider.reboot_device, self._on_power_action_done, self._on_power_action_failed)

    def _do_shutdown(self) -> None:
        self.btn_power.setEnabled(False)
        self._show_toast("กำลังปิดเครื่อง...", success=True, duration_ms=4000)
        self._start_task(self.provider.shutdown_device, self._on_power_action_done, self._on_power_action_failed)

    def _on_power_action_done(self, result: object) -> None:
        # เครื่องกำลังจะรีสตาร์ท/ปิดตัวเอง ไม่ต้องอัปเดต UI เพิ่ม
        pass

    def _on_power_action_failed(self, message: str) -> None:
        self.btn_power.setEnabled(True)
        self._show_toast(f"ดำเนินการไม่สำเร็จ: {message}", success=False, duration_ms=3000)

    def _open_wifi_selector(self) -> None:
        if self.network_task and self.network_task.isRunning():
            self._show_toast("กำลังสแกนหรือเชื่อมต่อเครือข่ายอยู่", success=False, duration_ms=1800)
            return
        if SubsystemRegistry.get("wifi").disabled:
            SubsystemRegistry.get("wifi").enable()
            self._show_toast("กำลังลองเชื่อมต่อ Wi-Fi ใหม่...", success=True, duration_ms=1500)
        self._show_toast("กำลังสแกน Wi-Fi...", success=True, duration_ms=1200)
        self._start_network_task(self.provider.scan_wifi_networks, self._on_wifi_scan_done, self._on_wifi_action_failed)

    def _on_wifi_scan_done(self, result: object) -> None:
        self.network_task = None
        networks = list(result) if isinstance(result, list) else []
        if not networks:
            self._show_toast("ไม่พบ Wi-Fi ที่เลือกได้", success=False)
            return

        ssid, ok = self._select_from_list("เลือก Wi-Fi", "Wi-Fi network:", networks)
        if not ok or not ssid:
            return

        password, ok = self._ask_password("รหัสผ่าน Wi-Fi", f"Password for {ssid}:")
        if not ok:
            return

        self._show_toast(f"กำลังเชื่อมต่อ Wi-Fi: {ssid}", success=True, duration_ms=1200)
        self._start_network_task(
            lambda: self.provider.connect_wifi(ssid, password or None),
            lambda result: self._on_network_connected("เชื่อมต่อ Wi-Fi สำเร็จ"),
            self._on_wifi_action_failed,
        )

    def _open_bluetooth_selector(self) -> None:
        if self.network_task and self.network_task.isRunning():
            self._show_toast("กำลังสแกนหรือเชื่อมต่ออุปกรณ์อยู่", success=False, duration_ms=1800)
            return
        if SubsystemRegistry.get("bluetooth").disabled:
            SubsystemRegistry.get("bluetooth").enable()
            self._show_toast("กำลังลองเชื่อมต่อ Bluetooth ใหม่...", success=True, duration_ms=1500)
        self._show_toast("กำลังสแกน Bluetooth...", success=True, duration_ms=1200)
        self._start_network_task(
            self.provider.scan_bluetooth_devices,
            self._on_bluetooth_scan_done,
            self._on_bluetooth_action_failed,
        )

    def _on_bluetooth_scan_done(self, result: object) -> None:
        self.network_task = None
        devices = list(result) if isinstance(result, list) else []
        if not devices:
            self._show_toast("ไม่พบ Bluetooth device ที่เลือกได้", success=False)
            return

        labels = [f"{name} ({address})" for name, address in devices]
        selected, ok = self._select_from_list("เลือก Bluetooth", "Bluetooth device:", labels)
        if not ok or not selected:
            return

        index = labels.index(selected)
        address = devices[index][1]
        self._show_toast(f"กำลังเชื่อมต่อ Bluetooth: {address}", success=True, duration_ms=1200)
        self._start_network_task(
            lambda: self.provider.connect_bluetooth(address),
            lambda result: self._on_network_connected("เชื่อมต่อ Bluetooth สำเร็จ"),
            self._on_bluetooth_action_failed,
        )

    def _on_network_connected(self, message: str) -> None:
        self._show_toast(message, success=True)
        self._request_device_status()

    def _on_wifi_action_failed(self, message: str) -> None:
        self._show_toast(f"Wi-Fi: {message}", success=False, duration_ms=3000)

    def _on_bluetooth_action_failed(self, message: str) -> None:
        self._show_toast(f"Bluetooth: {message}", success=False, duration_ms=3000)

    def _on_retry_attempt(self, subsystem: str, attempt: int, max_attempts: int) -> None:
        label = _SUBSYSTEM_LABELS.get(subsystem, subsystem)
        self._show_toast(f"{label}: กำลังลองใหม่ ({attempt}/{max_attempts})", success=False, duration_ms=1800)

    def _on_retry_giveup(self, subsystem: str, reason: str) -> None:
        label = _SUBSYSTEM_LABELS.get(subsystem, subsystem)
        self._show_toast(f"{label}: ปิดใช้งานชั่วคราวหลังลองใหม่ไม่สำเร็จ", success=False, duration_ms=3000)
        self._request_device_status()

    def _on_queue_drain_success(self, row_id: int) -> None:
        self._show_toast("ส่งข้อมูลที่ค้างอยู่สำเร็จแล้ว", success=True, duration_ms=2000)

    def _on_queue_drain_failure(self, row_id: int, error: str) -> None:
        now = time.monotonic()
        if now - self.last_queue_warning_ts < 60:
            return
        self.last_queue_warning_ts = now
        self._show_toast("ยังส่งข้อมูลที่ค้างอยู่ไม่ได้ ระบบจะลองใหม่อัตโนมัติ", success=False, duration_ms=2600)

    def _show_summary(self) -> None:
        self._refresh_values()
        if hasattr(self, "history_panel"):
            self.history_panel.hide()
            self.summary_table.show()
            self.btn_history.setText("ดูข้อมูลย้อนหลัง")
        self.stack.setCurrentIndex(2)

    @staticmethod
    def _format_int(value: int | None) -> str:
        return "--" if value is None else str(value)

    @staticmethod
    def _format_cid(value: str) -> str:
        digits = "".join(ch for ch in value if ch.isdigit())
        if len(digits) == 13:
            return f"{digits[0]}-{digits[1:5]}-{digits[5:10]}-{digits[10:12]}-{digits[12]}"
        return value

    def _format_manual_cid_input(self, text: str) -> None:
        if getattr(self, "_formatting_manual_cid", False):
            return

        invalid_chars = any(not ch.isdigit() and ch not in "- " for ch in text)
        digits = "".join(ch for ch in text if ch.isdigit())[:13]
        parts = [digits[:1], digits[1:5], digits[5:10], digits[10:12], digits[12:13]]
        formatted = "-".join(part for part in parts if part)

        if hasattr(self, "lbl_manual_cid_error"):
            self.lbl_manual_cid_error.setText("กรุณากรอกเฉพาะตัวเลข 0-9 เท่านั้น" if invalid_chars else "")

        if formatted == text:
            return

        self._formatting_manual_cid = True
        self.txt_manual_cid.setText(formatted)
        self.txt_manual_cid.setCursorPosition(len(formatted))
        self._formatting_manual_cid = False

    def _set_measure_button(self, button: QPushButton, object_name: str, text: str, enabled: bool = True) -> None:
        button.setObjectName(object_name)
        button.setText(text)
        button.setEnabled(enabled)
        button.style().unpolish(button)
        button.style().polish(button)

    # Redesign override methods for the dark medical-console UI.
    def _status_cluster(self, welcome: bool = False) -> QFrame:
        frame = QFrame()
        frame.setObjectName("StatusCluster")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        bt = BluetoothIndicator()
        wifi = WifiIndicator()
        battery = BatteryIndicator()
        battery_text = QLabel("0%")
        battery_text.setObjectName("ConsoleBatteryLabel")
        bt_text = QLabel("OFF")
        bt_text.setObjectName("StatusText")
        wifi_text = QLabel("OFF")
        wifi_text.setObjectName("StatusText")

        wifi.clicked.connect(self._open_wifi_selector)
        bt.clicked.connect(self._open_bluetooth_selector)

        bt_card = QFrame()
        bt_card.setObjectName("StatusPill")
        bt_card.setFixedSize(116, 42)
        bt_layout = QHBoxLayout(bt_card)
        bt_layout.setContentsMargins(8, 4, 10, 4)
        bt_layout.setSpacing(4)
        bt_layout.addWidget(bt, alignment=Qt.AlignCenter)
        bt_layout.addWidget(bt_text, alignment=Qt.AlignCenter)

        wifi_card = QFrame()
        wifi_card.setObjectName("StatusPill")
        wifi_card.setFixedSize(92, 42)
        wifi_layout = QHBoxLayout(wifi_card)
        wifi_layout.setContentsMargins(8, 4, 10, 4)
        wifi_layout.setSpacing(4)
        wifi_layout.addWidget(wifi, alignment=Qt.AlignCenter)
        wifi_layout.addWidget(wifi_text, alignment=Qt.AlignCenter)

        battery_card = QFrame()
        battery_card.setObjectName("BatteryPill")
        battery_card.setFixedSize(100, 42)
        battery_layout = QHBoxLayout(battery_card)
        battery_layout.setContentsMargins(10, 4, 10, 4)
        battery_layout.setSpacing(6)
        battery_layout.addWidget(battery, alignment=Qt.AlignCenter)
        battery_layout.addWidget(battery_text, alignment=Qt.AlignCenter)

        layout.addWidget(bt_card)
        layout.addWidget(wifi_card)
        layout.addWidget(battery_card)

        if not hasattr(self, "_status_widgets"):
            self._status_widgets = []
        self._status_widgets.append((bt, wifi, battery, battery_text, bt_text, wifi_text))

        if welcome:
            self.bt_ind_welcome = bt
            self.wifi_ind_welcome = wifi
            self.bat_ind_welcome = battery
            self.lbl_bat_welcome = battery_text
        else:
            self.bluetooth_indicator = bt
            self.wifi_indicator = wifi
            self.battery_indicator = battery
            self.lbl_battery_text = battery_text

        return frame

    def _on_status_done(self, result: object) -> None:
        if not isinstance(result, DeviceStatus):
            return
        self.status_fail_count = 0

        for bt, wifi, battery, battery_text, bt_text, wifi_text in getattr(self, "_status_widgets", []):
            battery_text.setText("--%" if result.battery_percent is None else f"{result.battery_percent}%")
            battery.set_percent(result.battery_percent)
            # Wi-Fi/Bluetooth are absent on the measure and summary headers.
            if wifi is not None:
                wifi.set_connected(result.wifi_connected and not result.wifi_disabled)
                wifi_text.setText(
                    "ปิดใช้งาน" if result.wifi_disabled
                    else ("ON" if result.wifi_connected else "OFF")
                )
            if bt is not None:
                bt.set_connected(result.bluetooth_connected and not result.bluetooth_disabled)
                bt_text.setText(
                    "ปิดใช้งาน" if result.bluetooth_disabled
                    else ("CONNECTED" if result.bluetooth_connected else "OFF")
                )

        if hasattr(self, "btn_bp") and self.bp_cooldown_seconds == 0:
            if result.bp_disabled:
                self._set_measure_button(self.btn_bp, "BtnNIBPDisabled", "ปิดใช้งาน\nความดัน", True)
            elif self.btn_bp.objectName() == "BtnNIBPDisabled":
                self._set_measure_button(self.btn_bp, "BtnNIBP", "เริ่มวัดค่า\nความดัน", True)

        if hasattr(self, "btn_spo2"):
            if result.spo2_disabled:
                self._set_measure_button(self.btn_spo2, "BtnSpO2Disabled", "ปิดใช้งาน\nออกซิเจน", True)
            elif self.btn_spo2.objectName() == "BtnSpO2Disabled":
                self._set_measure_button(self.btn_spo2, "BtnSpO2Console", "เริ่มวัดค่า\nออกซิเจน", True)

        if hasattr(self, "btn_card"):
            if result.idcard_disabled:
                self.btn_card.setText("ปิดใช้งาน (กดเพื่อลองใหม่)")
            elif self.btn_card.text() == "ปิดใช้งาน (กดเพื่อลองใหม่)":
                self.btn_card.setText("อ่านข้อมูลบัตร")

    def _on_status_failed(self, message: str) -> None:
        self.status_fail_count += 1
        now = time.monotonic()
        if self.status_fail_count == 1 or now - self.last_status_warning_ts >= 60:
            self.last_status_warning_ts = now
            self._show_toast("อ่านสถานะอุปกรณ์ไม่ได้ ระบบจะลองใหม่อัตโนมัติ", success=False, duration_ms=2400)

    def _read_card(self) -> None:
        if SubsystemRegistry.get("idcard").disabled:
            SubsystemRegistry.get("idcard").enable()
        self.btn_card.setText("กำลังอ่านข้อมูลบัตร...")
        self.btn_card.setEnabled(False)
        self._set_system_message("กำลังอ่านข้อมูลจากบัตรประชาชน", success=None)
        self._start_task(self.provider.read_patient, self._on_patient_read, self._on_patient_failed)

    def _show_manual_cid_entry(self) -> None:
        self.txt_manual_cid.clear()
        self.lbl_manual_cid_error.setText("")
        self._set_system_message("กรอกเลขบัตรประชาชน 13 หลักเมื่อเครื่องอ่านบัตรไม่พร้อมใช้งาน", success=None)
        self.manual_cid_dialog.show()
        self.manual_cid_dialog.raise_()
        self.manual_cid_dialog.activateWindow()
        self.txt_manual_cid.setFocus(Qt.OtherFocusReason)

    def _hide_manual_cid_entry(self) -> None:
        self.manual_cid_dialog.hide()
        self.txt_manual_cid.clear()
        self._set_system_message("พร้อมอ่านข้อมูลบัตร", success=None)

    def _submit_manual_cid(self) -> None:
        raw_text = self.txt_manual_cid.text()
        if any(not ch.isdigit() and ch not in "- " for ch in raw_text):
            self.lbl_manual_cid_error.setText("กรุณากรอกเฉพาะตัวเลข 0-9 เท่านั้น")
            self._show_popup("กรุณากรอกเฉพาะตัวเลข 0-9 เท่านั้น", success=False, duration_ms=2200)
            return

        cid = "".join(ch for ch in self.txt_manual_cid.text() if ch.isdigit())
        if len(cid) != 13:
            self.lbl_manual_cid_error.setText("กรุณากรอกเลขบัตรประชาชนให้ครบ 13 หลัก")
            self._set_system_message("กรุณากรอกเลขบัตรประชาชนให้ครบ 13 หลัก", success=False)
            self._show_popup("กรุณากรอกเลขบัตรประชาชนให้ครบ 13 หลัก", success=False, duration_ms=2200)
            return

        self.lbl_manual_cid_error.setText("")
        self.patient = PatientInfo(
            cid=cid,
            th_name="--",
            en_name="-",
            birth_date="--",
            address="--",
        )
        self.vitals = VitalState()
        self._hide_manual_cid_entry()
        self.btn_card.setEnabled(True)
        self._refresh_patient()
        self._refresh_values()
        self._set_system_message("กรอกเลขบัตรประชาชนสำเร็จ", success=True)
        self.stack.setCurrentIndex(1)

    def _refresh_patient(self) -> None:
        # Thai name only. The status cluster (Bluetooth/Wi-Fi/battery) is back on
        # the measure and summary headers, so the English name is dropped to keep
        # row 1 from overrunning the cluster on the 1024px screen.
        display_name = self.patient.th_name
        display_address = self._short_address(self.patient.address)

        for name, cid, dob, address in (
            (self.lbl_name, self.lbl_cid, self.lbl_dob, self.lbl_address),
            (self.sum_lbl_name, self.sum_lbl_cid, self.sum_lbl_dob, self.sum_lbl_address),
        ):
            name.setText(display_name)
            cid.setText(f"| {self._format_cid(self.patient.cid)}")
            dob.setText(f"เกิด: {self.patient.birth_date}")
            address.setText(display_address)

    @staticmethod
    def _short_address(address: str) -> str:
        return (
            address
            .replace("ตำบล", "ต.")
            .replace("อำเภอ", "อ.")
            .replace("จังหวัด", "จ.")
        )

    def _console_label(self, text: str, object_name: str, alignment: Qt.AlignmentFlag = Qt.AlignLeft | Qt.AlignVCenter) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        label.setAlignment(alignment)
        return label

    def _metric_row(self, name: str, value_label: QLabel, unit: str, value_color_name: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        lbl_name = self._console_label(name, "MetricName")
        lbl_name.setFixedWidth(54)
        lbl_name.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        value_label.setObjectName(value_color_name)
        value_label.setAlignment(Qt.AlignCenter)
        value_label.setFixedWidth(240)

        unit_label = self._console_label(unit, "MetricUnit")
        unit_label.setFixedWidth(80)
        unit_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        row.addWidget(lbl_name)
        row.addStretch(1)
        row.addWidget(value_label)
        row.addSpacing(24)
        row.addWidget(unit_label)

        return row

    def _build_scan_page(self) -> None:
        root = QWidget()
        root.setObjectName("RootBg")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(12)

        top = QHBoxLayout()
        brand = self._console_label("HealthLink", "ScanBrand")
        top.addWidget(brand)
        top.addStretch()
        top.addWidget(self._status_cluster(welcome=True))
        self.btn_power = PowerButton()
        self.btn_power.clicked.connect(self._open_power_menu)
        top.addWidget(self.btn_power)
        outer.addLayout(top)

        card = QFrame()
        card.setObjectName("ScanPanel")
        card.setMinimumWidth(0)
        card.setMaximumWidth(16777215)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(58, 34, 58, 34)
        card_layout.setSpacing(18)

        title = self._console_label("สแกนบัตรประชาชน", "ScanTitle", Qt.AlignCenter)
        subtitle = self._console_label(
            "เสียบบัตรประชาชนบนเครื่องอ่านบัตรเพื่อเริ่มการตรวจ",
            "ScanSubtitle",
            Qt.AlignCenter,
        )
        subtitle.setWordWrap(True)
        self.scan_title = title
        self.scan_subtitle = subtitle

        self.scan_icon_frame = QFrame()
        self.scan_icon_frame.setObjectName("ScanIconFrame")
        self.scan_icon_frame.setFixedSize(96, 78)
        scan_icon_layout = QVBoxLayout(self.scan_icon_frame)
        scan_icon_layout.setContentsMargins(0, 0, 0, 0)
        scan_icon = QLabel()
        scan_icon.setAlignment(Qt.AlignCenter)
        scan_icon.setPixmap(_tinted_icon("id-card-svgrepo-com.svg", 54))
        scan_icon_layout.addWidget(scan_icon, alignment=Qt.AlignCenter)
        self.scan_icon_frame.hide()

        self.btn_card = QPushButton("อ่านข้อมูลบัตร")
        self.btn_card.setObjectName("BtnScanCard")
        self.btn_card.setFixedSize(520, 62)
        self.btn_card.clicked.connect(self._read_card)

        self.btn_manual_card = QLabel()
        self.btn_manual_card.setObjectName("BtnManualCard")
        self.btn_manual_card.setTextFormat(Qt.RichText)
        self.btn_manual_card.setTextInteractionFlags(Qt.NoTextInteraction)
        self.btn_manual_card.setCursor(Qt.PointingHandCursor)
        self.btn_manual_card.setAlignment(Qt.AlignCenter)
        self.btn_manual_card.setFixedWidth(520)
        self.btn_manual_card.setFixedHeight(38)
        self.btn_manual_card.setText(
            'กรณีอ่านไม่สำเร็จ กรุณากรอกเลขบัตรเอง '
            '<span style="color:#9aff2d;">คลิกที่นี่</span>'
        )
        self.btn_manual_card.mousePressEvent = lambda event: self._show_manual_cid_entry()

        self.manual_cid_panel = QFrame()
        self.manual_cid_panel.setObjectName("ManualCidPanel")
        manual_layout = QVBoxLayout(self.manual_cid_panel)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(2)

        manual_title = self._console_label(
            "กรุณากรอกเลขบัตรประจำตัวประชาชน 13 หลัก",
            "ManualCidTitle",
            Qt.AlignCenter,
        )
        manual_title.setWordWrap(True)

        self.manual_cid_panel = QFrame()
        self.manual_cid_panel.setObjectName("ManualCidPanel")
        manual_layout = QVBoxLayout(self.manual_cid_panel)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(8)

        self.txt_manual_cid = QLineEdit()
        self.txt_manual_cid.setObjectName("ManualCidInput")
        self.txt_manual_cid.setFocusPolicy(Qt.StrongFocus)
        self.txt_manual_cid.setMaxLength(17)
        self.txt_manual_cid.setAlignment(Qt.AlignCenter)
        self.txt_manual_cid.setPlaceholderText("0-0000-00000-00-0")
        self.txt_manual_cid.setFixedWidth(390)
        self.txt_manual_cid.setFixedHeight(48)
        self.txt_manual_cid.setInputMethodHints(Qt.ImhDigitsOnly)
        self.txt_manual_cid.textChanged.connect(self._format_manual_cid_input)
        self.txt_manual_cid.returnPressed.connect(self._submit_manual_cid)

        self.lbl_manual_cid_error = self._console_label("", "ManualCidError", Qt.AlignCenter)
        self.lbl_manual_cid_error.setFixedHeight(24)

        self.btn_confirm_manual_cid = QPushButton("ยืนยันข้อมูล")
        self.btn_confirm_manual_cid.setObjectName("BtnConfirmManualCid")
        self.btn_confirm_manual_cid.setFixedSize(210, 44)
        self.btn_confirm_manual_cid.clicked.connect(self._submit_manual_cid)

        self.btn_cancel_manual_cid = QPushButton("ย้อนกลับ")
        self.btn_cancel_manual_cid.setObjectName("BtnCancelManualCid")
        self.btn_cancel_manual_cid.setFixedSize(210, 44)
        self.btn_cancel_manual_cid.clicked.connect(self._hide_manual_cid_entry)

        manual_actions = QHBoxLayout()
        manual_actions.setContentsMargins(0, 0, 0, 0)
        manual_actions.setSpacing(12)
        manual_actions.addStretch(1)
        manual_actions.addWidget(self.btn_cancel_manual_cid)
        manual_actions.addWidget(self.btn_confirm_manual_cid)
        manual_actions.addStretch(1)
        manual_layout.addWidget(manual_title)
        manual_layout.addWidget(self.txt_manual_cid, alignment=Qt.AlignCenter)
        manual_layout.addWidget(self.lbl_manual_cid_error)

        self.manual_cid_keypad = self._build_numeric_keypad(self.txt_manual_cid)
        manual_layout.addWidget(self.manual_cid_keypad, alignment=Qt.AlignCenter)

        manual_layout.addSpacing(18)
        manual_layout.addLayout(manual_actions)

        # A real top-level dialog (like the Wi-Fi/Bluetooth prompts) so the
        # touchscreen on-screen keyboard auto-shows; an inline embedded field
        # in the main window never gets the focus event the keyboard watches for.
        self.manual_cid_dialog = QDialog(self)
        self.manual_cid_dialog.setWindowTitle("กรอกเลขบัตรประชาชน")
        self.manual_cid_dialog.setModal(True)
        self.manual_cid_dialog.setFixedSize(620, 470)
        self.manual_cid_dialog.setStyleSheet("QDialog { background-color: #050709; }")
        dialog_layout = QVBoxLayout(self.manual_cid_dialog)
        dialog_layout.setContentsMargins(28, 10, 28, 12)
        dialog_layout.addWidget(self.manual_cid_panel)

        card_layout.addStretch(1)
        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(18)
        card_layout.addWidget(self.btn_card, alignment=Qt.AlignCenter)
        card_layout.addWidget(self.btn_manual_card, alignment=Qt.AlignCenter)
        card_layout.addStretch(1)

        outer.addWidget(card, 1)
        self.stack.addWidget(root)

    def _build_dashboard_page(self) -> None:
        root = QWidget()
        root.setObjectName("RootBg")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        layout.addWidget(self._build_patient_header(summary=False))

        panel = QFrame()
        panel.setObjectName("ConsolePanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)

        measure = QFrame()
        measure.setObjectName("MeasureGrid")
        measure_layout = QHBoxLayout(measure)
        measure_layout.setContentsMargins(0, 0, 0, 0)
        measure_layout.setSpacing(0)

        self.btn_bp = QPushButton("เริ่มวัดค่า\nความดัน")
        self.btn_bp.setObjectName("BtnNIBP")
        self.btn_bp.setFixedWidth(112)
        self.btn_bp.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.btn_bp.clicked.connect(self._measure_bp)
        measure_layout.addWidget(self.btn_bp)

        nibp = QFrame()
        nibp.setObjectName("NibpSection")
        nibp_layout = QVBoxLayout(nibp)
        nibp_layout.setContentsMargins(28, 10, 26, 4)
        nibp_layout.setSpacing(0)

        nibp_layout.addWidget(self._console_label("NIBP", "SectionTitleYellow"))
        nibp_layout.addSpacing(6)

        self.lbl_sys_value = self._console_label("--", "ValueYellow")
        self.lbl_dia_value = self._console_label("--", "ValueYellow")
        self.lbl_pulse_value = self._console_label("--", "ValuePulsePink")

        nibp_layout.addLayout(self._metric_row("SYS", self.lbl_sys_value, "mmHg", "ValueYellow"), 10)
        nibp_layout.addLayout(self._metric_row("DIA", self.lbl_dia_value, "mmHg", "ValueYellow"), 10)
        nibp_layout.addLayout(self._metric_row("PUL", self.lbl_pulse_value, "bpm", "ValuePulsePink"), 10)

        nibp_layout.addStretch(1)

        measure_layout.addWidget(nibp, 5)

        right = QFrame()
        right.setObjectName("RightMeasureColumn")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        spo2_row = QFrame()
        spo2_row.setObjectName("RightMetricRow")
        spo2_layout = QHBoxLayout(spo2_row)
        spo2_layout.setContentsMargins(22, 10, 0, 0)
        spo2_layout.setSpacing(0)
        spo2_box = QVBoxLayout()
        spo2_box.setSpacing(4)
        spo2_box.addWidget(self._console_label("SPO2", "SectionTitleBlue"))
        spo2_value_row = QHBoxLayout()
        spo2_value_row.setSpacing(14)
        self.lbl_spo2_value = self._console_label("--", "ValueBlue")
        self.lbl_spo2_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_spo2_value.setFixedWidth(176)
        spo2_unit = self._console_label("%", "MetricUnitLarge")
        spo2_unit.setFixedWidth(56)
        spo2_value_row.addWidget(self.lbl_spo2_value)
        spo2_value_row.addWidget(spo2_unit)
        spo2_value_row.addStretch()
        spo2_box.addLayout(spo2_value_row)
        spo2_box.addStretch(1)
        spo2_layout.addLayout(spo2_box, 1)
        self.btn_spo2 = QPushButton("เริ่มวัดค่า\nออกซิเจน")
        self.btn_spo2.setObjectName("BtnSpO2Console")
        self.btn_spo2.setFixedWidth(112)
        self.btn_spo2.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.btn_spo2.clicked.connect(self._measure_spo2)
        spo2_layout.addWidget(self.btn_spo2)
        right_layout.addWidget(spo2_row, 1)

        temp_row = QFrame()
        temp_row.setObjectName("RightMetricRow")
        temp_layout = QHBoxLayout(temp_row)
        temp_layout.setContentsMargins(22, 10, 0, 0)
        temp_layout.setSpacing(0)
        temp_box = QVBoxLayout()
        temp_box.setSpacing(4)
        temp_box.addWidget(self._console_label("TEMP", "SectionTitleGreen"))
        temp_value_row = QHBoxLayout()
        temp_value_row.setSpacing(10)
        self.lbl_temp_value = self._console_label("--", "ValueGreen")
        self.lbl_temp_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_temp_value.setFixedWidth(176)
        temp_unit = self._console_label("°C", "MetricUnitLarge")
        temp_unit.setFixedWidth(62)
        temp_value_row.addWidget(self.lbl_temp_value)
        temp_value_row.addWidget(temp_unit)
        temp_value_row.addStretch()
        temp_box.addLayout(temp_value_row)
        temp_box.addStretch(1)
        temp_layout.addLayout(temp_box, 1)
        self.btn_temp = QPushButton("เริ่มวัดค่า\nอุณหภูมิ")
        self.btn_temp.setObjectName("BtnTempConsole")
        self.btn_temp.setFixedWidth(112)
        self.btn_temp.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.btn_temp.clicked.connect(self._measure_temperature)
        temp_layout.addWidget(self.btn_temp)
        right_layout.addWidget(temp_row, 1)

        measure_layout.addWidget(right, 6)
        panel_layout.addWidget(measure, 1)

        footer = QFrame()
        footer.setObjectName("ConsoleFooter")
        footer.setMinimumHeight(72)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(20, 10, 18, 10)
        footer_layout.setSpacing(16)
        # Same "back to home" control as the summary page, bottom-left, so the
        # operator can bail out of a measurement without finishing it.
        self.btn_back_home_measure = QPushButton("ย้อนกลับหน้าแรก")
        self.btn_back_home_measure.setObjectName("BtnBack")
        self.btn_back_home_measure.setFixedSize(210, 50)
        self.btn_back_home_measure.clicked.connect(self._reset_session)
        footer_layout.addWidget(self.btn_back_home_measure)
        # Elides so that, with the back button now sharing the row, a long status
        # line shrinks instead of pushing the buttons off a narrow screen.
        self.lbl_system_message = ElidedLabel("สถานะ: รอคำสั่งวัดค่า")
        self.lbl_system_message.setObjectName("SystemMessageNeutral")
        self.lbl_system_message.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_measure_count = self._console_label("วัดค่าสำเร็จแล้ว 0 รายการ", "FooterHint")
        footer_layout.addWidget(self.lbl_system_message, 2)
        footer_layout.addWidget(self.lbl_measure_count, 1)
        self.btn_summary = QPushButton("สรุปผลการวัด  >")
        self.btn_summary.setObjectName("BtnSummaryDisabled")
        self.btn_summary.setFixedSize(320, 54)
        self.btn_summary.setEnabled(False)
        self.btn_summary.clicked.connect(self._show_summary)
        footer_layout.addWidget(self.btn_summary)
        panel_layout.addWidget(footer)

        layout.addWidget(panel, 1)
        self.stack.addWidget(root)

    def _build_summary_page(self) -> None:
        root = QWidget()
        root.setObjectName("RootBg")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        layout.addWidget(self._build_patient_header(summary=True))

        panel = QFrame()
        panel.setObjectName("SummaryPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(24, 18, 24, 16)
        panel_layout.setSpacing(12)

        top = QHBoxLayout()
        top.addWidget(self._console_label("ข้อมูลผลการวัด (Measurement Summary)", "SummaryTitle"))
        top.addStretch()
        btn_remeasure = QPushButton("เริ่มวัดอีกครั้ง")
        self.btn_history = QPushButton("ดูข้อมูลย้อนหลัง")
        self.btn_history.setObjectName("BtnSummarySmall")
        self.btn_history.setFixedSize(160, 42)
        self.btn_history.clicked.connect(self._request_history)
        top.addWidget(self.btn_history)
        btn_remeasure.setObjectName("BtnSummarySmall")
        btn_remeasure.setFixedSize(190, 42)
        btn_remeasure.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        top.addWidget(btn_remeasure)
        panel_layout.addLayout(top)

        table = QFrame()
        table.setObjectName("SummaryTable")
        self.summary_table = table
        grid = QGridLayout(table)
        grid.setContentsMargins(46, 16, 46, 16)
        grid.setHorizontalSpacing(26)
        grid.setVerticalSpacing(20)

        self.sum_bp_value = self._console_label("--/--", "SummaryValueYellow", Qt.AlignRight | Qt.AlignVCenter)
        self.sum_pulse_value = self._console_label("--", "SummaryValuePulsePink", Qt.AlignRight | Qt.AlignVCenter)
        self.sum_spo2_value = self._console_label("--", "SummaryValueBlue", Qt.AlignRight | Qt.AlignVCenter)
        self.sum_temp_value = self._console_label("--", "SummaryValueGreen", Qt.AlignRight | Qt.AlignVCenter)
        for value_label in (self.sum_bp_value, self.sum_pulse_value, self.sum_spo2_value, self.sum_temp_value):
            value_label.setFixedWidth(190)

        rows = [
            ("ความดันโลหิต Blood Pressure", self.sum_bp_value, "mmHg"),
            ("อัตราการเต้นของหัวใจ Pulse", self.sum_pulse_value, "bpm"),
            ("ออกซิเจนในเลือด Oxygen Saturation", self.sum_spo2_value, "%"),
            ("อุณหภูมิร่างกาย Body Temperature", self.sum_temp_value, "°C"),
        ]
        rows = [
            ("ความดันโลหิต (BP)", self.sum_bp_value, "mmHg"),
            ("อัตราการเต้นของหัวใจ (Pulse)", self.sum_pulse_value, "bpm"),
            ("ออกซิเจนในเลือด (SpO2)", self.sum_spo2_value, "%"),
            ("อุณหภูมิร่างกาย (Temp)", self.sum_temp_value, "°C"),
        ]
        for row_index, (name, value, unit) in enumerate(rows):
            grid.addWidget(self._console_label(name, "SummaryName"), row_index, 0)
            grid.addWidget(value, row_index, 1)
            grid.addWidget(self._console_label(unit, "SummaryUnit", Qt.AlignLeft | Qt.AlignVCenter), row_index, 2)

        grid.setColumnStretch(0, 4)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(2, 2)
        panel_layout.addWidget(table, 1)

        self.history_panel = QFrame()
        self.history_panel.setObjectName("HistoryPanel")
        history_layout = QVBoxLayout(self.history_panel)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(8)

        history_table = QFrame()
        history_table.setObjectName("HistoryTable")
        history_grid = QGridLayout(history_table)
        history_grid.setContentsMargins(0, 0, 0, 0)
        history_grid.setHorizontalSpacing(0)
        history_grid.setVerticalSpacing(0)

        headers = [
            "วันที่และเวลา",
            "ความดันโลหิต\n(mmHg)",
            "ชีพจร\n(bpm)",
            "ออกซิเจนในเลือด\n(SpO2 %)",
            "อุณหภูมิ\n(°C)",
        ]
        for column, header_text in enumerate(headers):
            header_label = self._console_label(header_text, "HistoryHeader", Qt.AlignCenter)
            header_label.setWordWrap(True)
            header_label.setMinimumHeight(42)
            history_grid.addWidget(header_label, 0, column)

        self.history_cells: list[list[QLabel]] = []
        cell_names = [
            "HistoryDate",
            "HistoryValueYellow",
            "HistoryValuePink",
            "HistoryValueBlue",
            "HistoryValueGreen",
        ]
        for row in range(4):
            row_cells: list[QLabel] = []
            for column, object_name in enumerate(cell_names):
                cell = self._console_label("-", object_name, Qt.AlignCenter)
                cell.setWordWrap(True)
                cell.setMinimumHeight(46)
                history_grid.addWidget(cell, row + 1, column)
                row_cells.append(cell)
            self.history_cells.append(row_cells)

        history_grid.setColumnStretch(0, 2)
        history_grid.setColumnStretch(1, 2)
        history_grid.setColumnStretch(2, 1)
        history_grid.setColumnStretch(3, 2)
        history_grid.setColumnStretch(4, 2)
        history_layout.addWidget(history_table, 1)
        self.history_panel.hide()
        panel_layout.addWidget(self.history_panel)

        self.lbl_summary_system_message = self._console_label("สถานะ: ตรวจสอบข้อมูลก่อนบันทึก", "SystemMessageNeutral")
        panel_layout.addWidget(self.lbl_summary_system_message)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 8, 0, 0)
        footer.setSpacing(14)
        self.btn_back_home = QPushButton("ย้อนกลับหน้าแรก")
        self.btn_back_home.setObjectName("BtnBack")
        self.btn_back_home.setFixedSize(210, 50)
        self.btn_back_home.clicked.connect(self._reset_session)
        footer.addWidget(self.btn_back_home)
        footer.addStretch()
        self.btn_finish = QPushButton("บันทึกข้อมูล  >")
        self.btn_finish.setObjectName("BtnFinish")
        self.btn_finish.setFixedSize(320, 54)
        self.btn_finish.clicked.connect(self._submit_data)
        footer.addWidget(self.btn_finish)
        panel_layout.addLayout(footer)

        layout.addWidget(panel, 1)
        self.stack.addWidget(root)

    def _build_patient_header(self, summary: bool) -> QFrame:
        header = QFrame()
        header.setObjectName("ConsoleHeader")
        header.setFixedHeight(82)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 10, 14, 10)
        layout.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(4)
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        # Name elides with "…" so a long name can never overrun the CID beside
        # it; the CID keeps its natural width and stays fully visible.
        lbl_name = ElidedLabel("-")
        lbl_name.setObjectName("HeaderNameConsole")
        lbl_name.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_cid = self._console_label("-", "HeaderCidConsole")
        row1.addWidget(lbl_name)
        row1.addWidget(lbl_cid)
        row1.addStretch()
        row2 = QHBoxLayout()
        row2.setSpacing(12)
        lbl_dob = self._console_label("เกิด: -", "HeaderSubConsole")
        lbl_address = ElidedLabel("-")
        lbl_address.setObjectName("HeaderSubConsole")
        lbl_address.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        row2.addWidget(lbl_dob)
        row2.addWidget(lbl_address, 1)
        info.addLayout(row1)
        info.addLayout(row2)
        layout.addLayout(info, 1)
        layout.addWidget(self._status_cluster(welcome=False))
        
        if summary:
            self.sum_lbl_name = lbl_name
            self.sum_lbl_cid = lbl_cid
            self.sum_lbl_dob = lbl_dob
            self.sum_lbl_address = lbl_address
        else:
            self.lbl_name = lbl_name
            self.lbl_cid = lbl_cid
            self.lbl_dob = lbl_dob
            self.lbl_address = lbl_address

        return header

    def _measured_count(self) -> int:
        return sum(
            1
            for done in (
                self.vitals.systolic is not None and self.vitals.diastolic is not None,
                self.vitals.spo2 is not None,
                self.vitals.temperature is not None,
            )
            if done
        )

    def _set_system_message(self, message: str, success: bool | None = None) -> None:
        object_name = "SystemMessageNeutral"
        prefix = "สถานะ"
        if success is True:
            object_name = "SystemMessageSuccess"
            prefix = "สำเร็จ"
        elif success is False:
            object_name = "SystemMessageFail"
            prefix = "ไม่สำเร็จ"

        text = f"{prefix}: {message}"
        for label_name in ("lbl_system_message", "lbl_summary_system_message"):
            label = getattr(self, label_name, None)
            if label is None:
                continue
            if label.objectName() == object_name and label.text() == text:
                continue
            label.setObjectName(object_name)
            label.setText(text)
            label.style().unpolish(label)
            label.style().polish(label)

    def _request_history(self) -> None:
        if self.history_panel.isVisible():
            self.history_panel.hide()
            self.summary_table.show()
            self.btn_history.setText("ดูข้อมูลย้อนหลัง")
            return

        self.btn_history.setEnabled(False)
        self.btn_history.setText("กำลังโหลด...")
        self._start_task(
            lambda: self.provider.get_measurement_history(self.patient.cid),
            self._on_history_done,
            self._on_history_failed,
        )

    @staticmethod
    def _format_history_time(value: object) -> str:
        text = "-" if value in (None, "") else str(value)
        if " " in text:
            date_part, time_part = text.rsplit(" ", 1)
            return f"{date_part}\n{time_part}"
        return text

    @staticmethod
    def _format_history_number(value: object, decimals: int = 0) -> str:
        if value in (None, ""):
            return "-"
        if decimals > 0:
            return f"{float(value):.{decimals}f}"
        return str(value)

    def _set_history_row(self, row: int, record: MeasurementHistoryRecord | None) -> None:
        cells = self.history_cells[row]
        if record is None:
            for cell in cells:
                cell.setText("-")
            return

        bp_text = "-"
        if record.systolic is not None and record.diastolic is not None:
            bp_text = f"{record.systolic}/{record.diastolic}"

        cells[0].setText(self._format_history_time(record.measured_at))
        cells[1].setText(bp_text)
        cells[2].setText(self._format_history_number(record.pulse))
        cells[3].setText(self._format_history_number(record.spo2))
        cells[4].setText(self._format_history_number(record.temperature, 1))

    def _on_history_done(self, result: object) -> None:
        records = [record for record in (result if isinstance(result, list) else []) if isinstance(record, MeasurementHistoryRecord)]
        records = records[:4]
        for row in range(4):
            record = records[row] if row < len(records) else None
            self._set_history_row(row, record)

        self.summary_table.hide()
        self.history_panel.show()
        self.btn_history.setEnabled(True)
        self.btn_history.setText("สรุปผลการวัด")

    def _on_history_failed(self, message: str) -> None:
        self.history_panel.show()
        self.summary_table.hide()
        for row in range(4):
            self._set_history_row(row, None)
        self.history_cells[0][0].setText("โหลดข้อมูล\nไม่สำเร็จ")
        self.history_cells[0][1].setText("-")
        self.btn_history.setEnabled(True)
        self.btn_history.setText("สรุปผลการวัด")
        self._show_toast("โหลดข้อมูลย้อนหลังไม่สำเร็จ", success=False, duration_ms=2400)

    def _refresh_values(self) -> None:
        sys_text = self._format_int(self.vitals.systolic)
        dia_text = self._format_int(self.vitals.diastolic)
        bp_text = "--/--"
        if self.vitals.systolic is not None and self.vitals.diastolic is not None:
            bp_text = f"{self.vitals.systolic}/{self.vitals.diastolic}"

        pulse_text = self._format_int(self.vitals.pulse)
        spo2_text = self._format_int(self.vitals.spo2)
        temp_text = "--" if self.vitals.temperature is None else f"{self.vitals.temperature:.1f}"

        if hasattr(self, "lbl_sys_value"):
            self.lbl_sys_value.setText(sys_text)
            self.lbl_dia_value.setText(dia_text)
            self.lbl_pulse_value.setText(pulse_text)
            self.lbl_spo2_value.setText(spo2_text)
            self.lbl_temp_value.setText(temp_text)

        self.sum_bp_value.setText(bp_text)
        self.sum_pulse_value.setText(pulse_text)
        self.sum_spo2_value.setText(spo2_text)
        self.sum_temp_value.setText(temp_text)

        self._refresh_summary_badges()
        self._refresh_summary_button()

    def _refresh_summary_button(self) -> None:
        count = self._measured_count()
        has_data = count > 0
        if hasattr(self, "lbl_measure_count"):
            self.lbl_measure_count.setText(f"วัดค่าสำเร็จแล้ว {count} รายการ")
        self.btn_summary.setEnabled(has_data)
        self.btn_summary.setObjectName("BtnSummaryReady" if has_data else "BtnSummaryDisabled")
        self.btn_summary.setText("สรุปผลการวัด  >" if has_data else "รอผลการวัด")
        self.btn_summary.style().unpolish(self.btn_summary)
        self.btn_summary.style().polish(self.btn_summary)

    def _refresh_summary_badges(self) -> None:
        return

    def _on_patient_read(self, result: object) -> None:
        self.patient = result if isinstance(result, PatientInfo) else PatientInfo()
        self.vitals = VitalState()
        self.btn_card.setText("อ่านข้อมูลบัตร")
        self.btn_card.setEnabled(True)
        self._refresh_patient()
        self._refresh_values()
        self._set_system_message("อ่านข้อมูลบัตรสำเร็จ", success=True)
        self.stack.setCurrentIndex(1)

    def _on_patient_failed(self, message: str) -> None:
        self.btn_card.setText("อ่านข้อมูลบัตร")
        self.btn_card.setEnabled(True)
        self._show_manual_cid_entry()
        self._set_system_message(f"อ่านบัตรไม่สำเร็จ: {message}", success=False)
        self._show_popup(f"อ่านบัตรไม่สำเร็จ: {message}", success=False, duration_ms=2500)

    def _measure_bp(self) -> None:
        if self.bp_cooldown_seconds > 0:
            return
        if SubsystemRegistry.get("bp_monitor").disabled:
            SubsystemRegistry.get("bp_monitor").enable()
        self._set_measure_button(self.btn_bp, "BtnNIBPBusy", "กำลังวัด\nความดัน", False)
        self._set_system_message("กำลังวัดความดันโลหิต", success=None)
        self._begin_measurement_display("bp")
        self._start_task(self.provider.measure_blood_pressure, self._on_bp_done, self._on_bp_failed)

    def _on_bp_done(self, result: object) -> None:
        if isinstance(result, BloodPressureReading):
            self.vitals.systolic = result.systolic
            self.vitals.diastolic = result.diastolic
            self.vitals.pulse = result.pulse
        self._finish_measurement_display("bp")
        self._start_bp_cooldown(self.BP_COOLDOWN_AFTER_SUCCESS, succeeded=True)
        self._set_system_message("วัดความดันโลหิตสำเร็จ", success=True)
        self._refresh_values()

    def _on_bp_failed(self, message: str) -> None:
        self._set_measure_button(self.btn_bp, "BtnNIBPFail", "วัดไม่สำเร็จ\nความดัน", True)
        self._set_system_message(f"วัดความดันไม่สำเร็จ: {message}", success=False)
        self._cancel_measurement_display("bp")
        # A BP_ERROR parks the cuff in a lockout of its own, so give it a
        # moment rather than letting the operator hammer the button into a
        # device that is not listening. See BP_COOLDOWN_AFTER_DEVICE_ERROR for
        # why that pause is shorter than the lockout itself.
        if getattr(self.provider, "last_bp_error", None) == "BP_ERROR":
            self._start_bp_cooldown(self.BP_COOLDOWN_AFTER_DEVICE_ERROR, succeeded=False)

    def _start_bp_cooldown(self, seconds: int, *, succeeded: bool) -> None:
        self.bp_cooldown_seconds = seconds
        self.bp_cooldown_succeeded = succeeded
        self.cooldown_timer.start(1000)
        self._set_measure_button(self.btn_bp, "BtnNIBPBusy", f"รอ\n{seconds} วินาที", False)

    def _measure_spo2(self) -> None:
        if SubsystemRegistry.get("spo2").disabled:
            SubsystemRegistry.get("spo2").enable()
        self._set_measure_button(self.btn_spo2, "BtnSpO2Busy", "กำลังวัด\nออกซิเจน", False)
        self._set_system_message("กำลังวัดออกซิเจนในเลือด", success=None)
        self._begin_measurement_display("spo2")
        self._start_task(self.provider.measure_spo2, self._on_spo2_done, self._on_spo2_failed)

    def _on_spo2_done(self, result: object) -> None:
        self.vitals.spo2 = int(result)
        self._finish_measurement_display("spo2", result)
        self._set_measure_button(self.btn_spo2, "BtnSpO2Done", "วัดแล้ว\nออกซิเจน", True)
        self._set_system_message("วัดออกซิเจนในเลือดสำเร็จ", success=True)
        self._refresh_values()

    def _on_spo2_failed(self, message: str) -> None:
        self._set_measure_button(self.btn_spo2, "BtnSpO2Fail", "วัดไม่สำเร็จ\nออกซิเจน", True)
        self._set_system_message(f"วัดออกซิเจนไม่สำเร็จ: {message}", success=False)
        self._cancel_measurement_display("spo2")

    def _measure_temperature(self) -> None:
        self._set_measure_button(self.btn_temp, "BtnTempBusy", "กำลังวัด\nอุณหภูมิ", False)
        self._set_system_message("กำลังวัดอุณหภูมิร่างกาย", success=None)
        self._begin_measurement_display("temp")
        self._start_task(self.provider.measure_temperature, self._on_temperature_done, self._on_temperature_failed)

    def _on_temperature_done(self, result: object) -> None:
        self.vitals.temperature = float(result)
        self._finish_measurement_display("temp", result)
        self._set_measure_button(self.btn_temp, "BtnTempDone", "วัดแล้ว\nอุณหภูมิ", True)
        self._set_system_message("วัดอุณหภูมิร่างกายสำเร็จ", success=True)
        self._refresh_values()

    def _on_temperature_failed(self, message: str) -> None:
        self._set_measure_button(self.btn_temp, "BtnTempFail", "วัดไม่สำเร็จ\nอุณหภูมิ", True)
        self._set_system_message(f"วัดอุณหภูมิไม่สำเร็จ: {message}", success=False)
        self._cancel_measurement_display("temp")

    def _reset_session(self) -> None:
        for kind in self.measurement_active:
            self.measurement_active[kind] = False
        self.bp_sys_live_display.stop()
        self.bp_dia_live_display.stop()
        self.bp_pulse_live_display.stop()
        self.spo2_live_display.stop()
        self.temp_live_display.stop()
        self.patient = PatientInfo()
        self.vitals = VitalState()
        self.bp_cooldown_seconds = 0
        self.bp_cooldown_succeeded = True
        self.cooldown_timer.stop()
        self.btn_bp.setEnabled(True)
        self.btn_bp.setText("เริ่มวัดค่า\nความดัน")
        self.btn_spo2.setEnabled(True)
        self.btn_spo2.setText("เริ่มวัดค่า\nออกซิเจน")
        self.btn_temp.setEnabled(True)
        self.btn_temp.setText("เริ่มวัดค่า\nอุณหภูมิ")
        self.btn_finish.setEnabled(True)
        self.btn_finish.setText("บันทึกข้อมูล  >")
        self._set_measure_button(self.btn_bp, "BtnNIBP", "เริ่มวัดค่า\nความดัน", True)
        self._set_measure_button(self.btn_spo2, "BtnSpO2Console", "เริ่มวัดค่า\nออกซิเจน", True)
        self._set_measure_button(self.btn_temp, "BtnTempConsole", "เริ่มวัดค่า\nอุณหภูมิ", True)
        if hasattr(self, "manual_cid_dialog"):
            self.manual_cid_dialog.hide()
            self.btn_manual_card.show()
            self.btn_card.show()
            self.scan_title.show()
            self.scan_subtitle.show()
            self.scan_icon_frame.hide()
            self.txt_manual_cid.clear()
        self._refresh_patient()
        self._refresh_values()
        self._set_system_message("พร้อมอ่านข้อมูลบัตร", success=None)
        self.stack.setCurrentIndex(0)

    def _bp_cooldown_tick(self) -> None:
        if self.bp_cooldown_seconds > 0:
            self.bp_cooldown_seconds -= 1
            if self.bp_cooldown_seconds > 0:
                self._set_measure_button(self.btn_bp, "BtnNIBPBusy", f"รอ\n{self.bp_cooldown_seconds} วินาที", False)
                return
        self.cooldown_timer.stop()
        # Come back to the state the cooldown started from: a cooldown can now
        # follow a failure too, and that must not end up reading "วัดแล้ว".
        if self.bp_cooldown_succeeded:
            self._set_measure_button(self.btn_bp, "BtnNIBPDone", "วัดแล้ว\nความดัน", True)
        else:
            self._set_measure_button(self.btn_bp, "BtnNIBPFail", "วัดไม่สำเร็จ\nความดัน", True)

    def _submit_data(self) -> None:
        patient_id = "".join(ch for ch in self.patient.cid if ch.isdigit())
        payload = {
            "mac": getattr(self.provider, "device_mac", "unknown") or "unknown",
            "patient_id": patient_id,
            "spo2": self.vitals.spo2,
            "heart_rate": self.vitals.pulse,
            "pr_bpm": self.vitals.pulse,
            "sys": self.vitals.systolic,
            "dia": self.vitals.diastolic,
            "pulse": self.vitals.pulse,
        }

        self.btn_finish.setText("กำลังบันทึกข้อมูล...")
        self.btn_finish.setEnabled(False)
        self._set_system_message("กำลังส่งข้อมูลเข้าสู่ระบบ", success=None)

        # Enqueue immediately (fast local disk write) so the measurement is
        # never lost even if the immediate send below fails. The background
        # QueueDrainWorker guarantees eventual delivery either way.
        row_id = self.submission_queue.enqueue(payload)
        self._start_task(
            lambda: self._try_immediate_send(row_id, payload),
            self._on_submit_success,
            self._on_submit_failed,
        )

    def _try_immediate_send(self, row_id: int, payload: dict) -> bool:
        ok = self.provider.send_data(payload)
        if ok:
            self.submission_queue.mark_sent_and_delete(row_id)
        return ok

    def _on_submit_success(self, result: object) -> None:
        self.btn_finish.setEnabled(True)
        self.btn_finish.setText("บันทึกข้อมูล  >")
        self._set_system_message("บันทึกข้อมูลสัญญาณชีพสำเร็จ", success=True)
        self._show_popup("บันทึกข้อมูลสำเร็จ", success=True)
        QTimer.singleShot(2000, self._reset_session)

    def _on_submit_failed(self, message: str) -> None:
        # Data is already safely queued (mark_failed leaves it pending) — the
        # background QueueDrainWorker will retry it, so the operator can
        # proceed immediately instead of waiting around for the network.
        self.btn_finish.setEnabled(True)
        self.btn_finish.setText("บันทึกข้อมูล  >")
        self._set_system_message("เครือข่ายขัดข้อง ข้อมูลถูกบันทึกไว้และจะส่งอัตโนมัติ", success=False)
        self._show_popup("ส่งข้อมูลไม่สำเร็จ ระบบจะลองส่งใหม่อัตโนมัติ", success=False, duration_ms=3000)
        QTimer.singleShot(2000, self._reset_session)

    def _apply_styles(self) -> None:
        self.setStyleSheet(build_stylesheet(APP_FONT_FAMILY, NUMBER_FONT_FAMILY))

def run_app(provider: CareKeeperProvider, mode_name: str = "Mock") -> None:
    global APP_FONT_FAMILY, NUMBER_FONT_FAMILY

    app = QApplication(sys.argv)
    log_thread_identity("main")
    APP_FONT_FAMILY = _load_app_font(app)
    NUMBER_FONT_FAMILY = _load_number_font()
    window = CareKeeperWindow(provider, mode_name=mode_name)
    window.showFullScreen()
    
    sys.exit(app.exec())
