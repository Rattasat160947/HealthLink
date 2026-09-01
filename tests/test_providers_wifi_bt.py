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
    monkeypatch.setattr(cp.subprocess, "check_output", lambda *a, **k: "")

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
    monkeypatch.setattr(cp.subprocess, "check_output", lambda *a, **k: "")

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
    monkeypatch.setattr(cp.subprocess, "check_output", lambda *a, **k: "")

    attempts = []
    provider.on_retry_attempt = lambda s, a, m: attempts.append((s, a, m))

    ok = provider.connect_wifi("HiddenNet", "secret")
    assert ok is True
    assert attempts == []  # succeeded on the first attempt via the hidden retry
    hidden_connects = [c for c in commands if "connect" in c and "hidden" in c]
    assert hidden_connects and hidden_connects[-1][-2:] == ["hidden", "yes"]
    assert SubsystemRegistry.get("wifi").disabled is False


def test_connect_wifi_forgets_stale_profile_before_connecting(provider, monkeypatch):
    """A changed router password only takes effect if the stale NM profile is
    deleted first; otherwise `nmcli device wifi connect` reuses its old stored
    password. The delete must happen before the connect."""
    monkeypatch.setattr(cp.subprocess, "check_output", lambda *a, **k: "")
    commands = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        return None  # delete, rescan and connect all succeed

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    ok = provider.connect_wifi("MyNet", "newpass")
    assert ok is True

    delete_cmd = ["nmcli", "connection", "delete", "id", "MyNet"]
    assert delete_cmd in commands
    connect_idx = next(i for i, c in enumerate(commands) if "connect" in c)
    assert commands.index(delete_cmd) < connect_idx


def test_connect_wifi_does_not_forget_profile_without_password(provider, monkeypatch):
    """An open network (no password) must not have its saved profile deleted --
    there is no new secret to apply, so deleting would only churn NM state."""
    monkeypatch.setattr(cp.subprocess, "check_output", lambda *a, **k: "")
    commands = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        return None

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    assert provider.connect_wifi("OpenNet") is True
    assert not any(c[:3] == ["nmcli", "connection", "delete"] for c in commands)


def test_connect_wifi_restores_previous_connection_when_connect_fails(provider, monkeypatch):
    """A failed connect must bring the previously active WiFi back up, so a
    wrong password never leaves the Pi offline and locked out of SSH/VNC."""
    monkeypatch.setattr(
        cp.subprocess, "check_output", lambda *a, **k: "OldNet:802-11-wireless\n"
    )
    commands = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        if "connect" in cmd:
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="bad password")
        return None

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError):
        provider.connect_wifi("NewNet", "whatever")

    assert ["nmcli", "connection", "up", "id", "OldNet"] in commands


def test_connect_wifi_no_restore_when_reconnecting_same_network(provider, monkeypatch):
    """When the network we fail to join *is* the currently active one, there is
    nothing safe to restore -- we must not try to bring the same broken profile
    back up."""
    monkeypatch.setattr(
        cp.subprocess, "check_output", lambda *a, **k: "SameNet:802-11-wireless\n"
    )
    commands = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        if "connect" in cmd:
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="bad password")
        return None

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError):
        provider.connect_wifi("SameNet", "whatever")

    assert not any(c[:3] == ["nmcli", "connection", "up"] for c in commands)


def test_scan_wifi_networks_retry_and_disable(provider, monkeypatch):
    calls = {"n": 0}

    def fake_check_output(*args, **kwargs):
        calls["n"] += 1
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: None)  # neutralize rescan
    monkeypatch.setattr(cp.subprocess, "check_output", fake_check_output)

    with pytest.raises(RuntimeError):
        provider.scan_wifi_networks()
    assert calls["n"] == 3
    assert SubsystemRegistry.get("wifi").disabled is True


def test_scan_wifi_networks_rescans_then_returns_all_ssids(provider, monkeypatch):
    """The scan forces a fresh rescan first, then returns every SSID the list
    reports -- all APs in range, not only saved/known networks."""
    ran = []

    def fake_run(cmd, *args, **kwargs):
        ran.append(cmd)
        return None

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cp.subprocess, "check_output",
        lambda *a, **k: "HomeWiFi\nCafe_Guest\nNeighbor_5G\n",
    )

    networks = provider.scan_wifi_networks()
    assert networks == ["HomeWiFi", "Cafe_Guest", "Neighbor_5G"]
    assert any("--rescan" in c and "yes" in c for c in ran)  # a rescan was forced first


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
