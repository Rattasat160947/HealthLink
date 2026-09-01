# -*- coding: utf-8 -*-
"""หา "รหัสข้อผิดพลาด" ของเครื่องวัดความดัน แล้วจดว่ารหัสไหนหมายถึงอะไร

โมดูลจบรอบที่วัดไม่ผ่านด้วย "end test,err:<n>" และ firmware ส่งต่อเป็น
"BP_ERROR:<n>" — ตัวเลข <n> คือคำตอบเดียวที่แยก "แขนขยับ" ออกจาก "ผ้าพันหลวม"
ออกจาก "ลมรั่ว" ได้ แต่ตารางความหมายของผู้ผลิตไม่ได้อยู่ในโปรเจกต์นี้
สคริปต์นี้จึงให้ "สร้างตารางเอง" จากการทดลองจริง ครั้งละหนึ่งสถานการณ์

    python sensor_tests/bp_error_codes.py "ผ้าพันแขนหลวม"
    python sensor_tests/bp_error_codes.py "ขยับแขนระหว่างวัด"
    python sensor_tests/bp_error_codes.py "ไม่ได้พันแขนเลย"
    python sensor_tests/bp_error_codes.py "ถอดสายลม" --port /dev/ttyUSB0

⚠ สคริปต์นี้สั่งวัดจริง ผ้าพันแขนจะบีบลมจริง และต้องเว้นแต่ละรอบ ~2 นาที
   ให้โมดูลปิดตัวก่อนรอบถัดไป (ถ้ากดเร็วไปจะได้ NOT_READY ซึ่งไม่ใช่ผลที่ต้องการ)

ทุกรอบจะถูกต่อท้ายไว้ที่ bp-error-codes.log ข้าง ๆ ไฟล์นี้ และถ้าได้รหัสมา
สคริปต์จะพิมพ์บรรทัดที่เอาไปวางใน RealCareKeeperProvider._BP_DEVICE_CODE_MESSAGES
ได้เลย

หมายเหตุ: ต้องแฟลช firmware รุ่นที่ส่ง "BP_ERROR:<n>" ก่อน ถ้าบอร์ดยังเป็นรุ่นเก่า
สคริปต์จะบอกว่าไม่มีรหัสติดมา (bare BP_ERROR)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from _project_path import ensure_project_root

ensure_project_root()

from lib.bp_monitor import BPMonitor  # noqa: E402

LOG_PATH = Path(__file__).resolve().parent / "bp-error-codes.log"


def _log(line: str) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="สถานการณ์ที่จัดไว้สำหรับรอบนี้ เช่น 'ผ้าพันแขนหลวม'")
    parser.add_argument("--port", default="/dev/ttyUSB0", help="serial port ของบอร์ด ESP32")
    args = parser.parse_args()

    print(f"[bp-codes] สถานการณ์: {args.label}")
    print(f"[bp-codes] พอร์ต: {args.port}")
    print("[bp-codes] เริ่มวัด — ผ้าพันแขนกำลังจะบีบลม\n")

    monitor = BPMonitor(port=args.port)
    monitor.connect()
    try:
        result = monitor.measure()
        reason = monitor.last_error
        code = monitor.error_code
        busy_state = monitor.busy_state
    finally:
        monitor.disconnect()

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if result:
        print(f"\n[bp-codes] รอบนี้วัดสำเร็จ ({result}) — ไม่ได้รหัสข้อผิดพลาด")
        print("[bp-codes] ถ้าต้องการรหัส ต้องจัดสถานการณ์ให้วัดไม่ผ่านจริง ๆ")
        _log(f"{stamp}\t{args.label}\tOK\t{result}")
        return 1

    if reason == BPMonitor.ERR_NOT_READY:
        print(f"\n[bp-codes] โมดูลยังไม่ว่าง (state: {busy_state or 'unknown'})")
        print("[bp-codes] รออีกสักครู่แล้วรันใหม่ — รอบนี้ยังไม่ได้วัดจริง")
        _log(f"{stamp}\t{args.label}\tNOT_READY\t{busy_state or ''}")
        return 1

    if reason != BPMonitor.ERR_DEVICE:
        print(f"\n[bp-codes] รอบนี้จบด้วย {reason} ซึ่งไม่ใช่ข้อผิดพลาดที่โมดูลแจ้งเอง")
        print("[bp-codes] ไม่มีรหัสให้บันทึก")
        _log(f"{stamp}\t{args.label}\t{reason}\t")
        return 1

    if code is None:
        print("\n[bp-codes] โมดูลแจ้งข้อผิดพลาด แต่ firmware ส่งมาเป็น BP_ERROR เปล่า ๆ")
        print("[bp-codes] แปลว่าบอร์ดยังเป็น firmware รุ่นเก่า ต้องแฟลชรุ่นที่ส่ง BP_ERROR:<n> ก่อน")
        _log(f"{stamp}\t{args.label}\tBP_ERROR\t(no code)")
        return 1

    print(f"\n[bp-codes] ได้รหัสแล้ว: {code}  ←  '{args.label}'")
    print("[bp-codes] เอาบรรทัดนี้ไปวางใน RealCareKeeperProvider._BP_DEVICE_CODE_MESSAGES:\n")
    print(f'        {code}: "เครื่องวัดความดัน: {args.label} กรุณาแก้ไขแล้ววัดใหม่",\n')
    print(f"[bp-codes] บันทึกไว้ที่ {LOG_PATH}")
    _log(f"{stamp}\t{args.label}\tBP_ERROR\t{code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
