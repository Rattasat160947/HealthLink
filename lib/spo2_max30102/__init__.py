# -*- coding: utf-8 -*-
"""MAX30102 pulse-oximeter driver (from the E-Medhealth sensor library).

MAX30102 I2C pulse-oximeter support. Import is kept
lazy in carekeeper_providers (this package pulls in `smbus`/`numpy`, which only
exist on the Pi), so importing carekeeper_providers on a dev box stays cheap."""
from .spo2_monitor import SpO2Monitor

__all__ = ["SpO2Monitor"]
