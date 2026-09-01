# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

import pytest

from carekeeper_retry import SubsystemRegistry, retry_with_notify, retry_with_notify_async


def _fail_n_then_succeed(n, result="ok"):
    calls = {"count": 0}

    def action():
        calls["count"] += 1
        if calls["count"] <= n:
            raise RuntimeError(f"boom #{calls['count']}")
        return result

    action.calls = calls
    return action


def _always_fail():
    def action():
        raise RuntimeError("always fails")

    return action


def test_retry_succeeds_on_first_attempt():
    action = _fail_n_then_succeed(0)
    attempts_seen = []
    result = retry_with_notify(
        action, subsystem="test_a", delay_seconds=0,
        on_attempt=lambda a, m: attempts_seen.append((a, m)),
    )
    assert result == "ok"
    assert attempts_seen == []
    assert SubsystemRegistry.get("test_a").disabled is False


def test_retry_succeeds_on_second_attempt():
    action = _fail_n_then_succeed(1)
    attempts_seen = []
    result = retry_with_notify(
        action, subsystem="test_b", delay_seconds=0,
        on_attempt=lambda a, m: attempts_seen.append((a, m)),
    )
    assert result == "ok"
    assert attempts_seen == [(2, 3)]
    assert SubsystemRegistry.get("test_b").disabled is False


def test_retry_exhausts_and_disables():
    action = _always_fail()
    give_up_reasons = []
    with pytest.raises(RuntimeError):
        retry_with_notify(
            action, subsystem="test_c", delay_seconds=0,
            on_give_up=lambda r: give_up_reasons.append(r),
        )
    assert len(give_up_reasons) == 1
    state = SubsystemRegistry.get("test_c")
    assert state.disabled is True
    assert state.consecutive_failures == 3


def test_retry_respects_custom_max_attempts():
    call_count = {"n": 0}

    def action():
        call_count["n"] += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        retry_with_notify(action, subsystem="test_d", max_attempts=5, delay_seconds=0)
    assert call_count["n"] == 5


def test_retry_success_clears_prior_disabled_state():
    with pytest.raises(RuntimeError):
        retry_with_notify(_always_fail(), subsystem="test_e", delay_seconds=0)
    assert SubsystemRegistry.get("test_e").disabled is True

    result = retry_with_notify(_fail_n_then_succeed(0), subsystem="test_e", delay_seconds=0)
    assert result == "ok"
    assert SubsystemRegistry.get("test_e").disabled is False


def test_subsystem_registry_is_isolated_per_name():
    with pytest.raises(RuntimeError):
        retry_with_notify(_always_fail(), subsystem="wifi_iso", delay_seconds=0)
    assert SubsystemRegistry.get("wifi_iso").disabled is True
    assert SubsystemRegistry.get("network_iso").disabled is False


def test_retry_with_notify_async_succeeds_on_second_attempt():
    async def run():
        action = _fail_n_then_succeed(1)

        async def async_action():
            return action()

        attempts_seen = []
        result = await retry_with_notify_async(
            async_action, subsystem="test_async_a", delay_seconds=0,
            on_attempt=lambda a, m: attempts_seen.append((a, m)),
        )
        return result, attempts_seen

    result, attempts_seen = asyncio.run(run())
    assert result == "ok"
    assert attempts_seen == [(2, 3)]


def test_retry_with_notify_async_exhausts_and_disables():
    async def always_fail_async():
        raise RuntimeError("always fails async")

    async def run():
        with pytest.raises(RuntimeError):
            await retry_with_notify_async(always_fail_async, subsystem="test_async_b", delay_seconds=0)

    asyncio.run(run())
    assert SubsystemRegistry.get("test_async_b").disabled is True
