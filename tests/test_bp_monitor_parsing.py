# -*- coding: utf-8 -*-
"""Unit tests for the blood-pressure monitor serial protocol (lib/bp_monitor.py).

The ESP32 bridge sends plain-text lines ("SYS:120,DIA:80,PUL:72", "READY",
"BP_ERROR", "NOT_READY"). These tests feed lines straight into the parser
and line handler, so no serial port or cuff hardware is required.
"""
from __future__ import annotations

import pytest

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


def test_handle_line_bp_error_unblocks_measure_immediately():
    """The device has already given its verdict. Waiting for the READY that
    only arrives after its ~2 min lockout made every failed cuff run burn the
    full 120 s measure() timeout."""
    monitor = _monitor()
    monitor._done_event.clear()

    monitor._handle_line("BP_ERROR")

    assert monitor._done_event.is_set()
    assert monitor.last_error == BPMonitor.ERR_DEVICE


def test_handle_line_not_ready_unblocks_without_ready_flag():
    monitor = _monitor()
    monitor._is_ready = False
    monitor._done_event.clear()

    monitor._handle_line("NOT_READY")

    assert monitor._done_event.is_set()
    assert monitor.is_ready is False
    assert monitor.last_error == BPMonitor.ERR_NOT_READY


def test_handle_line_ready_releases_the_boot_settle_wait():
    """connect() waits on _ready_event so a bridge that announces itself is
    not made to sit out the whole boot-settle delay."""
    monitor = _monitor()
    assert monitor._ready_event.is_set() is False

    monitor._handle_line("READY")

    assert monitor._ready_event.is_set()


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
    assert monitor.last_error == BPMonitor.ERR_NOT_READY


def test_measure_timeout_is_reported_as_its_own_reason():
    """A silent device and a device that answered BP_ERROR both leave measure()
    empty; last_error is what lets the caller tell them apart."""
    monitor = BPMonitor(port="/dev/null", timeout=0.05)

    assert monitor.measure() is None
    assert monitor.last_error == BPMonitor.ERR_TIMEOUT


# ── _read_loop: line framing ──────────────────────────────────────────────

class _ChunkedPort:
    """Serial stand-in that hands _read_loop a scripted sequence of raw reads,
    then stops the loop. It deliberately has no `in_waiting`: the read loop is
    blocking-read driven now, and polling in_waiting in a tight sleepless loop
    is what used to pin a core and hold the GIL for the whole measurement."""

    def __init__(self, chunks, monitor):
        self.is_open = True
        self._chunks = list(chunks)
        self._monitor = monitor

    def readline(self) -> bytes:
        if not self._chunks:
            self._monitor._running = False
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        self.is_open = False


def _run_read_loop(monitor, chunks):
    monitor._ser = _ChunkedPort(chunks, monitor)
    monitor._running = True
    monitor._read_loop()


def test_read_loop_reassembles_a_line_split_across_two_reads():
    monitor = _monitor()

    _run_read_loop(monitor, [b"SYS:120,DIA:80,", b"PUL:70\n"])

    assert monitor.last_result == BPResult(sys=120, dia=80, pul=70)


def test_read_loop_flushes_a_final_line_with_no_trailing_newline():
    """Firmware that omits the last newline still gets parsed: the fragment is
    held only until a read comes back empty, then taken as complete."""
    monitor = _monitor()

    _run_read_loop(monitor, [b"SYS:118,DIA:79,PUL:65"])

    assert monitor.last_result == BPResult(sys=118, dia=79, pul=65)


@pytest.mark.parametrize("terminator", [b"\n", b"\r\n", b"\r"])
def test_read_loop_accepts_any_line_terminator(terminator):
    """readline() only breaks on LF. Firmware that ends lines with a bare CR
    would pile up unparsed for as long as it kept talking, so the reader
    splits on CR, LF and CRLF alike."""
    monitor = _monitor()

    _run_read_loop(monitor, [b"SYS:121,DIA:82,PUL:66" + terminator])

    assert monitor.last_result == BPResult(sys=121, dia=82, pul=66)


def test_read_loop_handles_several_lines_arriving_in_one_read():
    """A quiet moment then a burst is one read, not one line per read."""
    monitor = _monitor()

    _run_read_loop(monitor, [b"NOT_READY\r\nSYS:110,DIA:70,PUL:58\r\nREADY\r\n"])

    assert monitor.last_result == BPResult(sys=110, dia=70, pul=58)
    assert monitor.is_ready is True


def test_read_loop_keeps_separate_lines_separate():
    monitor = _monitor()
    ready = []
    monitor.on_ready = lambda: ready.append(True)

    _run_read_loop(monitor, [b"SYS:130,DIA:85,PUL:60\n", b"READY\n"])

    assert monitor.last_result == BPResult(sys=130, dia=85, pul=60)
    assert monitor.is_ready is True
    assert ready == [True]
