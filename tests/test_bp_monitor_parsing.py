# -*- coding: utf-8 -*-
"""Unit tests for the blood-pressure monitor serial protocol (lib/bp_monitor.py).

The ESP32 bridge sends plain-text lines ("SYS:120,DIA:80,PUL:72", "READY",
"BP_ERROR", "NOT_READY"). These tests feed lines straight into the parser
and line handler, so no serial port or cuff hardware is required.
"""
from __future__ import annotations

from lib.bp_monitor import BPMonitor, BPResult


def _monitor(**callbacks):
    return BPMonitor(port="/dev/null", **callbacks)


# ── _parse_result: "SYS:...,DIA:...,PUL:..." → BPResult ──────────────────

def test_parse_result_reads_all_three_fields():
    result = BPMonitor._parse_result("SYS:120,DIA:80,PUL:72")
    assert result == BPResult(sys=120, dia=80, pul=72)


def test_parse_result_tolerates_spaces():
    result = BPMonitor._parse_result("SYS: 118 ,DIA: 79 ,PUL: 65")
    assert result == BPResult(sys=118, dia=79, pul=65)


def test_parse_result_returns_none_on_garbage():
    assert BPMonitor._parse_result("hello world") is None


def test_parse_result_returns_none_when_field_missing():
    assert BPMonitor._parse_result("SYS:120,DIA:80") is None


def test_parse_result_returns_none_on_non_numeric_value():
    assert BPMonitor._parse_result("SYS:abc,DIA:80,PUL:72") is None


def test_bp_result_str_contains_units():
    text = str(BPResult(sys=120, dia=80, pul=72))
    assert "120 mmHg" in text
    assert "80 mmHg" in text
    assert "72 bpm" in text


# ── _handle_line: protocol state machine ──────────────────────────────────

def test_handle_line_result_stores_last_result_and_fires_callback():
    received = []
    monitor = _monitor(on_result=received.append)

    monitor._handle_line("SYS:135,DIA:88,PUL:70")

    assert monitor.last_result == BPResult(sys=135, dia=88, pul=70)
    assert received == [BPResult(sys=135, dia=88, pul=70)]


def test_handle_line_result_unblocks_measure_without_waiting_for_ready():
    """The SYS reading is already valid, so it must unblock measure() on its own
    -- not wait for the later READY line. This removes the firmware's SYS->READY
    delay from what the operator sees on screen."""
    monitor = _monitor()
    monitor._done_event.clear()

    monitor._handle_line("SYS:120,DIA:80,PUL:70")

    assert monitor._done_event.is_set()


def test_handle_line_malformed_result_keeps_last_result_none():
    monitor = _monitor()
    monitor._handle_line("SYS:bad,DIA:88,PUL:70")
    assert monitor.last_result is None


def test_handle_line_ready_marks_monitor_ready_and_unblocks_measure():
    ready_calls = []
    monitor = _monitor(on_ready=lambda: ready_calls.append(True))
    monitor._is_ready = False
    monitor._done_event.clear()

    monitor._handle_line("READY")

    assert monitor.is_ready is True
    assert monitor._done_event.is_set()
    assert ready_calls == [True]


def test_handle_line_bp_error_records_error_and_fires_callback():
    errors = []
    monitor = _monitor(on_error=errors.append)

    monitor._handle_line("BP_ERROR")

    assert monitor._last_error == "BP_ERROR"
    assert errors == ["BP_ERROR"]


def test_handle_line_not_ready_unblocks_without_ready_flag():
    monitor = _monitor()
    monitor._is_ready = False
    monitor._done_event.clear()

    monitor._handle_line("NOT_READY")

    assert monitor._done_event.is_set()
    assert monitor.is_ready is False


def test_measurement_sequence_result_then_ready():
    """Full happy-path line sequence as the ESP32 actually sends it."""
    monitor = _monitor()
    monitor._is_ready = False
    monitor._done_event.clear()

    monitor._handle_line("SYS:122,DIA:81,PUL:68")
    monitor._handle_line("READY")

    assert monitor.last_result == BPResult(sys=122, dia=81, pul=68)
    assert monitor.is_ready is True
    assert monitor._done_event.is_set()


def test_measure_refuses_when_not_ready():
    monitor = _monitor()
    monitor._is_ready = False
    assert monitor.measure() is None
