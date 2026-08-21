import smbus
import subprocess
import time

class UPSHat:
    """
    Waveshare UPS HAT (E)
    I2C Address : 0x2D
    """

    ADDR = 0x2D

    STATUS_IDLE = "IDLE"
    STATUS_CHARGING = "CHARGING"
    STATUS_FAST_CHARGING = "FAST_CHARGING"
    STATUS_DISCHARGING = "DISCHARGING"

    def __init__(self, bus_num=1):
        self.bus = smbus.SMBus(bus_num)

    # ==========================================================
    # Low Level
    # ==========================================================
    def _read(self, reg, length):
        return self.bus.read_i2c_block_data(self.ADDR, reg, length)

    # ==========================================================
    # Status
    # ==========================================================
    def get_status(self):
        data = self._read(0x02, 1)
        if data[0] & 0x40:
            return self.STATUS_FAST_CHARGING

        elif data[0] & 0x80:
            return self.STATUS_CHARGING

        elif data[0] & 0x20:
            return self.STATUS_DISCHARGING

        return self.STATUS_IDLE

    # ==========================================================
    # VBUS
    # ==========================================================
    def get_vbus_voltage(self):
        data = self._read(0x10, 6)
        return data[0] | (data[1] << 8)

    def get_vbus_current(self):
        data = self._read(0x10, 6)
        return data[2] | (data[3] << 8)

    def get_vbus_power(self):
        data = self._read(0x10, 6)
        return data[4] | (data[5] << 8)

    # ==========================================================
    # Battery
    # ==========================================================
    def get_battery_voltage(self):
        data = self._read(0x20, 12)
        return data[0] | (data[1] << 8)

    def get_battery_current(self):
        data = self._read(0x20, 12)
        current = data[2] | (data[3] << 8)
        if current > 0x7FFF:
            current -= 0xFFFF

        return current

    def get_battery_percent(self):
        data = self._read(0x20, 12)
        return int(data[4] | (data[5] << 8))

    def get_remaining_capacity(self):
        data = self._read(0x20, 12)
        return data[6] | (data[7] << 8)

    def get_runtime_to_empty(self):
        data = self._read(0x20, 12)
        return data[8] | (data[9] << 8)

    def get_time_to_full(self):
        data = self._read(0x20, 12)
        return data[10] | (data[11] << 8)

    # ==========================================================
    # Cells
    # ==========================================================
    def get_cell_voltages(self):
        data = self._read(0x30, 8)
        return {
            "V1": data[0] | (data[1] << 8),
            "V2": data[2] | (data[3] << 8),
            "V3": data[4] | (data[5] << 8),
            "V4": data[6] | (data[7] << 8),
        }

    # ==========================================================
    # Power Management
    # ==========================================================
    def enable_auto_power_on(self):
        """
        เขียนค่า 0x55 ไปยัง Register 0x01
        """
        self.bus.write_byte_data(self.ADDR, 0x01, 0x55)

    def shutdown(self):
        self.enable_auto_power_on()
        subprocess.call(["sudo", "poweroff"])

    # ==========================================================
    # Utility
    # ==========================================================
    def get_all(self):
        current = self.get_battery_current()
        data = {
            "status": self.get_status(),

            "vbus_voltage": self.get_vbus_voltage(),
            "vbus_current": self.get_vbus_current(),
            "vbus_power": self.get_vbus_power(),

            "battery_voltage": self.get_battery_voltage(),
            "battery_current": current,
            "battery_percent": self.get_battery_percent(),
            "remaining_capacity": self.get_remaining_capacity(),

            "cells": self.get_cell_voltages(),
        }
        if current < 0:
            data["runtime_to_empty"] = self.get_runtime_to_empty()
        else:
            data["time_to_full"] = self.get_time_to_full()

        return data

if __name__ == "__main__":
    ups = UPSHat()

    while True:
        info = ups.get_all()
        print("=" * 50)
        print("Status :", info["status"])
        print("Percent :", info["battery_percent"], "%")
        print("Voltage :", info["battery_voltage"], "mV")
        print("Current :", info["battery_current"], "mA")
        print("Remaining Capacity :", info["remaining_capacity"], "mAh")
        print("Cells")
        print(info["cells"] ,'mV')
        print()
        time.sleep(2)