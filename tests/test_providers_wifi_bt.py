# -*- coding: utf-8 -*-
from __future__ import annotations

import subprocess

import pytest

import carekeeper_providers as cp
from carekeeper_retry import SubsystemRegistry


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(cp.RealCareKeeperProvider, "__init__", lambda self: None)
    p = cp.RealCareKeeperProvider()
    p.device_mac = "aa:bb:cc:dd:ee:ff"
    p.api_url = cp.TEST_API_URL
    p.history_api_url = cp.TEST_HISTORY_API_URL
    p.on_retry_attempt = None
    p.on_retry_giveup = None
    return p


def _wifi_run_factory(connect_fail_times: int):
    """Command-aware fake for subprocess.run. The best-effort rescan
    (`... wifi list --rescan yes`) always succeeds; only the `connect` command
    drives the retry logic, failing its first `connect_fail_times` calls."""
    connect_calls = {"n": 0}

    def fake_run(cmd, *args, **kwargs):
        if "connect" in cmd:
            connect_calls["n"] += 1
            if connect_calls["n"] <= connect_fail_times:
                raise subprocess.CalledProcessError(1, cmd, output="", stderr="fake nmcli failure")
        return None  # rescan and successful connects

    fake_run.connect_calls = connect_calls
    return fake_run


def test_connect_wifi_retries_and_succeeds(provider, monkeypatch):
    fake_run = _wifi_run_factory(connect_fail_times=2)
    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    attempts = []
    provider.on_retry_attempt = lambda s, a, m: attempts.append((s, a, m))

    ok = provider.connect_wifi("SomeSSID", "password")
    assert ok is True
    assert fake_run.connect_calls["n"] == 3
    assert attempts == [("wifi", 2, 3), ("wifi", 3, 3)]
    assert SubsystemRegistry.get("wifi").disabled is False


def test_connect_wifi_exhausts_and_disables(provider, monkeypatch):
    fake_run = _wifi_run_factory(connect_fail_times=99)
    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError):
        provider.connect_wifi("SomeSSID")

    assert fake_run.connect_calls["n"] == 3
    assert SubsystemRegistry.get("wifi").disabled is True
    assert provider.get_device_status().wifi_disabled is True


def test_connect_wifi_falls_back_to_hidden_on_key_mgmt_error(provider, monkeypatch):
    """A hidden AP yields the key-mgmt error on the plain connect; the provider
    must retry the same attempt with `hidden yes` and succeed without spending
    the outer retry budget."""
    commands = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        if "connect" in cmd and "hidden" not in cmd:
            raise subprocess.CalledProcessError(
                1, cmd, output="",
                stderr="Error: 802-11-wireless-security.key-mgmt: property is missing.",
            )
        return None  # rescan + hidden connect succeed

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    attempts = []
    provider.on_retry_attempt = lambda s, a, m: attempts.append((s, a, m))

    ok = provider.connect_wifi("HiddenNet", "secret")
    assert ok is True
    assert attempts == []  # succeeded on the first attempt via the hidden retry
    hidden_connects = [c for c in commands if "connect" in c and "hidden" in c]
    assert hidden_connects and hidden_connects[-1][-2:] == ["hidden", "yes"]
    assert SubsystemRegistry.get("wifi").disabled is False


def test_scan_wifi_networks_retry_and_disable(provider, monkeypatch):
    calls = {"n": 0}

    def fake_check_output(*args, **kwargs):
        calls["n"] += 1
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(cp.subprocess, "check_output", fake_check_output)

    with pytest.raises(RuntimeError):
        provider.scan_wifi_networks()
    assert calls["n"] == 3
    assert SubsystemRegistry.get("wifi").disabled is True


def test_connect_bluetooth_retry_and_disable(provider, monkeypatch):
    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        calls["n"] += 1
        if "connect" in args[0] and kwargs.get("check"):
            raise subprocess.CalledProcessError(1, args[0], output="", stderr="fail")
        return None

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError):
        provider.connect_bluetooth("AA:BB:CC:DD:EE:FF")
    assert SubsystemRegistry.get("bluetooth").disabled is True


def test_scan_bluetooth_devices_retry_and_disable(provider, monkeypatch):
    def fake_run(*args, **kwargs):
        return None

    def fake_check_output(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    monkeypatch.setattr(cp.subprocess, "check_output", fake_check_output)

    with pytest.raises(RuntimeError):
        provider.scan_bluetooth_devices()
    assert SubsystemRegistry.get("bluetooth").disabled is True


def test_wifi_status_check_not_retried(provider, monkeypatch):
    """Regression guard: the 6s-poll status check must never be wrapped in
    retry_with_notify, or it would stall the UI poll for several seconds
    during an outage."""
    calls = {"n": 0}

    def fake_check_output(*args, **kwargs):
        calls["n"] += 1
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(cp.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(cp.sys, "platform", "linux")

    result = provider._is_wifi_connected()
    assert result is False
    assert calls["n"] == 1


def test_bluetooth_status_check_not_retried(provider, monkeypatch):
    calls = {"n": 0}

    def fake_check_output(*args, **kwargs):
        calls["n"] += 1
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(cp.subprocess, "check_output", fake_check_output)

    result = provider._is_bluetooth_connected()
    assert result is False
    assert calls["n"] == 1
