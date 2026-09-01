# -*- coding: utf-8 -*-
from __future__ import annotations

import dataclasses

from carekeeper_providers import DeviceStatus


def test_device_status_has_disabled_fields():
    field_names = {f.name for f in dataclasses.fields(DeviceStatus)}
    expected = {
        "battery_percent",
        "battery_charging",
        "wifi_connected",
        "wifi_disabled",
        "bp_disabled",
        "spo2_disabled",
        "idcard_disabled",
    }
    assert expected.issubset(field_names)

    status = DeviceStatus()
    assert status.battery_charging is False
    assert status.wifi_disabled is False
    assert status.bp_disabled is False
    assert status.spo2_disabled is False
    assert status.idcard_disabled is False
