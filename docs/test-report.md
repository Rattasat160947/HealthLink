# รายงานผลการทดสอบซอฟต์แวร์ (Software Test Report) — CareKeeper

- **วันที่จัดทำ:** 4 กรกฎาคม 2569 (2026-07-04)
- **ผู้จัดทำ:** ทีมพัฒนา CareKeeper
- **ขอบเขต:** Unit test และ Integration test ระดับโมดูลของแอปพลิเคชัน CareKeeper (เครื่องวัดสุขภาพประจำจุดบริการ บน Raspberry Pi)

---

## 1. สรุปผลการทดสอบ (Executive Summary)

| ตัวชี้วัด | ค่าที่วัดได้ |
|---|---|
| จำนวนกรณีทดสอบ (test cases) ทั้งหมด | **161** |
| ผ่าน | **161 (100%)** |
| ไม่ผ่าน | 0 |
| ความครอบคลุมของโค้ด (code coverage) โมดูลตรรกะ | **79%** (1,128 statements, พลาด 242) |
| เวลาที่ใช้รันทั้งชุด | **~1.8 วินาที** |
| การพึ่งพาฮาร์ดแวร์/เครือข่ายจริง | **ไม่มี** — รันซ้ำได้ทุกเครื่อง (deterministic) |

> ก่อนปรับปรุงรอบนี้ ชุดทดสอบมี 52 กรณี (ผ่าน 49, ไม่ผ่าน 3) และ coverage อยู่ที่ **57%**
> หลังปรับปรุงเพิ่มเป็น 161 กรณี ผ่านทั้งหมด และ coverage เพิ่มขึ้น **+22 จุด เป็น 79%**

---

## 2. สภาพแวดล้อมและเครื่องมือที่ใช้ทดสอบ

| รายการ | รุ่น/ค่า |
|---|---|
| ภาษา | Python 3.14.0 |
| Test framework | pytest 9.1.1 |
| ทดสอบ GUI (Qt) | pytest-qt 4.5.0 |
| วัด coverage | pytest-cov 7.1.0 (coverage.py) |
| ระบบปฏิบัติการที่ใช้รัน | macOS (เครื่องพัฒนา) — ชุดทดสอบไม่ผูกกับ OS |

**หลักการสำคัญ:** ชุดทดสอบไม่แตะฮาร์ดแวร์และเครือข่ายจริงเลย โดยใช้ตัวแทน (test double / fake) ดังนี้

| ของจริง | ตัวแทนที่ใช้ในเทสต์ | ไฟล์ |
|---|---|---|
| เครื่องวัดความดัน (ESP32 ผ่าน Serial) | ป้อนบรรทัดข้อความโปรโตคอลตรงเข้า parser | `tests/test_bp_monitor_parsing.py` |
| นาฬิกา H59 (Bluetooth LE) | อุปกรณ์จำลองที่บันทึกคำสั่งและฉีดแพ็กเก็ตตอบกลับ | `tests/test_h59_*.py` |
| เครื่องอ่านบัตรประชาชน (PC/SC) | การเชื่อมต่อจำลองที่คืนค่า APDU ตามสคริปต์ | `tests/test_thaiidcard.py` |
| UPS HAT (I2C) | โมดูล `smbus` ปลอมที่กำหนดค่า register ได้ | `tests/test_ups.py` |
| Backend API (HTTP) | `requests.get/post` ปลอมที่บันทึกทุก call | `tests/fakes/fake_requests.py` |

---

## 3. จำนวนกรณีทดสอบแยกตามระบบย่อย

| ระบบย่อย | ไฟล์ทดสอบ | จำนวน |
|---|---|---:|
| นาฬิกา H59 (BLE): โครงแพ็กเก็ต/checksum | `test_h59_packets.py` | 7 |
| นาฬิกา H59: แปลงแพ็กเก็ตชีพจรและ SpO2 (ขอบเขตค่า) | `test_h59_notify_parsers.py` | 23 |
| นาฬิกา H59: ลำดับคำสั่งวัดค่า (warm-up → start → stop) | `test_h59_reader_flows.py` | 12 |
| นาฬิกา H59: ชั้นสื่อสาร BLE (routing, keepalive, fan-out) | `test_h59_device.py` | 9 |
| เครื่องวัดความดัน: โปรโตคอล Serial | `test_bp_monitor_parsing.py` | 13 |
| เครื่องวัดความดัน: retry ระดับ provider | `test_providers_bp.py` | 3 |
| บัตรประชาชน: แปลงวันเกิด พ.ศ. / TIS-620 / APDU | `test_thaiidcard.py` | 15 |
| บัตรประชาชน: retry ระดับ provider | `test_providers_idcard.py` | 2 |
| แบตเตอรี่ UPS: ถอดรหัส register I2C | `test_ups.py` | 15 |
| Backend: ส่งผลวัด (POST add_health) | `test_providers_send_data.py` | 8 |
| Backend: ดึงประวัติผลวัด (GET health_history) | `test_providers_history.py` | 9 |
| คิวออฟไลน์ (SQLite): จัดเก็บ/กู้คืน | `test_queue.py` | 7 |
| คิวออฟไลน์: worker ส่งข้อมูลเบื้องหลัง | `test_queue_worker.py` | 5 |
| กลไก retry และสถานะ subsystem | `test_retry_helper.py` | 8 |
| Wi-Fi / Bluetooth ของตัวเครื่อง | `test_providers_wifi_bt.py` | 8 |
| SpO2 ระดับ provider | `test_providers_spo2.py` | 3 |
| โครงสร้างสถานะอุปกรณ์ | `test_device_status_fields.py` | 1 |
| การทำงานข้าม thread (GUI ไม่ถูก block) | `test_threading.py` | 4 |
| GUI: สถานะ disabled และการกดปุ่ม | `test_ui_status_disabled.py` | 6 |
| GUI: ลำดับการบันทึกและส่งข้อมูล | `test_ui_submit_flow.py` | 3 |
| **รวม** | | **161** |

---

## 4. ความครอบคลุมของโค้ด (Code Coverage)

วัดเฉพาะโมดูลตรรกะ (business logic และตัวเชื่อมฮาร์ดแวร์) — ไม่รวม `carekeeper_ui.py` ซึ่งเป็นโค้ดจัดวางหน้าจอ Qt (มีการทดสอบพฤติกรรมแยกด้วย pytest-qt จำนวน 9 กรณีในหมวด GUI ข้างต้น)

| โมดูล | Statements | พลาด | Coverage |
|---|---:|---:|---:|
| `lib/h59_ble/__init__.py` | 5 | 0 | **100%** |
| `lib/thaiidcard/apdu.py` | 6 | 0 | **100%** |
| `lib/h59_ble/spo2.py` | 57 | 1 | **98%** |
| `lib/h59_ble/heart_rate.py` | 75 | 3 | **96%** |
| `carekeeper_retry.py` | 109 | 4 | **96%** |
| `carekeeper_queue.py` | 106 | 5 | **95%** |
| `carekeeper_logging.py` | 12 | 1 | **92%** |
| `lib/bp_monitor.py` | 117 | 15 | **87%** |
| `lib/ups.py` | 81 | 15 | **81%** |
| `lib/thaiidcard/card.py` | 62 | 18 | **71%** |
| `carekeeper_providers.py` | 379 | 136 | **64%** |
| `lib/h59_ble/device.py` | 119 | 44 | **63%** |
| **รวม** | **1,128** | **242** | **79%** |

**ส่วนที่เหลือที่ยังไม่ครอบคลุม** ส่วนใหญ่เป็นโค้ดที่ต้องมีฮาร์ดแวร์/OS จริง ได้แก่ การ scan และ connect BLE จริง (`device.py`), การเรียก `nmcli`/`bluetoothctl`/`iwgetid` บน Raspberry Pi (`carekeeper_providers.py`) และการเปิดพอร์ต Serial จริง (`bp_monitor.py`) ซึ่งเหมาะกับการทดสอบแบบ hardware-in-the-loop มากกว่า unit test (ดูหัวข้อ 7)

---

## 5. กรณีทดสอบเด่น (ตัวอย่างที่มีตัวเลขวัดได้)

ตารางนี้คัดกรณีที่แสดง "ค่าป้อน → ผลที่คาด" ชัดเจน เหมาะกับการอ้างอิงในเอกสาร (ทุกกรณีผลจริง = ผลที่คาด)

| รหัส | สิ่งที่ทดสอบ | ค่าป้อน | ผลที่คาด |
|---|---|---|---|
| TC-01 | ขอบเขตชีพจรต่ำสุดที่ยอมรับ | แพ็กเก็ต `1E` ค่า 30 bpm | รับค่า = 30 |
| TC-02 | ขอบเขตชีพจรสูงสุดที่ยอมรับ | แพ็กเก็ต `1E` ค่า 220 bpm | รับค่า = 220 |
| TC-03 | ปฏิเสธชีพจรนอกช่วง | 29 bpm และ 221 bpm | ไม่รับค่า (None) |
| TC-04 | ขอบเขต SpO2 ที่ยอมรับ | 70% และ 100% (แฟล็กวัดเสร็จ = 0x01) | รับค่าตามจริง |
| TC-05 | ปฏิเสธ SpO2 นอกช่วง | 69% และ 101% | ไม่รับค่า (None) |
| TC-06 | ไม่รับค่า SpO2 ระหว่างยังวัดไม่เสร็จ | แฟล็ก byte[4] = 0x00 | ไม่รับค่า (None) |
| TC-07 | โครงแพ็กเก็ตคำสั่ง H59 | คำสั่งใดๆ | ยาว 16 ไบต์, ไบต์สุดท้าย = checksum (ผลรวม mod 256) |
| TC-08 | ลำดับคำสั่งวัดชีพจร | เรียก `read()` | เขียน 5 คำสั่ง (warm-up 4 + start `1E 01`) แล้วตัดการเชื่อมต่อ 1 ครั้ง |
| TC-09 | ลำดับคำสั่งวัด SpO2 | เรียก `read()` | ส่ง start `69 03 01` และ stop `69 03 00` เสมอ (แม้ timeout) |
| TC-10 | แปลงผลเครื่องวัดความดัน | `"SYS:120,DIA:80,PUL:72"` | BPResult(120, 80, 72) |
| TC-11 | ข้อความผิดรูปแบบไม่ทำให้พัง | `"hello world"`, ค่าไม่ครบ, ตัวอักษรแทนตัวเลข | คืน None ทุกกรณี |
| TC-12 | แปลงวันเกิดบัตรประชาชน (พ.ศ.) | `"25320415"` | `"15 เมษายน 2532"` |
| TC-13 | วันเกิดที่บัตรบันทึกเฉพาะปี (วัน = 00) | `"25320400"` | คืนค่าดิบไม่แปลง |
| TC-14 | โปรโตคอล APDU: GET RESPONSE | การ์ดตอบ SW1 = 0x61, SW2 = 0x0D | ส่ง `00 C0 00 00 0D` ตามแล้วได้ข้อมูล 13 ไบต์ |
| TC-15 | ถอดรหัสแรงดันแบตเตอรี่ (little-endian) | ไบต์ `[0x10, 0x0F]` | 3,856 mV |
| TC-16 | กระแสไฟติดลบตอนจ่ายไฟ (two's complement) | ไบต์ `[0x0C, 0xFE]` | −499 mA |
| TC-17 | สถานะชาร์จจากบิตแฟล็ก | register 0x02 = 0x40 / 0x80 / 0x20 / 0x00 | FAST_CHARGING / CHARGING / DISCHARGING / IDLE |
| TC-18 | ส่งผลวัดขึ้น backend สำเร็จ | HTTP 200/201 | คืน True, แนบ header `api-key` |
| TC-19 | backend ปฏิเสธ | HTTP 401/500 | โยน RuntimeError |
| TC-20 | ดึงประวัติจำกัด 4 รายการ | ข้อมูล 6 รายการ | ได้ 4 รายการล่าสุด |
| TC-21 | คิวออฟไลน์คงข้อมูลข้ามการรีสตาร์ต | enqueue → เปิดคิวใหม่จากไฟล์เดิม | รายการยังอยู่ครบ |
| TC-22 | retry แล้วปิด subsystem เมื่อล้มเหลวครบ | ล้มเหลว 3 ครั้งติด | subsystem ถูก disable พร้อมเหตุผล |
| TC-23 | เชื่อม Wi-Fi ที่ซ่อนชื่อ (hidden SSID) | connect ปกติล้มเหลวด้วย `802-11-wireless-security.key-mgmt: property is missing` + มีรหัสผ่าน | retry อัตโนมัติด้วย `hidden yes` แล้วเชื่อมสำเร็จในความพยายามเดิม |

---

## 6. วิธีรันซ้ำเพื่อยืนยันตัวเลข (Reproducibility)

```bash
# ติดตั้งเครื่องมือ (ครั้งแรกครั้งเดียว)
pip install -r requirements-dev.txt

# รันชุดทดสอบทั้งหมด
python -m pytest tests/ -v

# รันพร้อมวัด coverage (ตัวเลขในหัวข้อ 1 และ 4)
python -m pytest tests/ \
  --cov=carekeeper_queue --cov=carekeeper_retry \
  --cov=carekeeper_providers --cov=carekeeper_logging \
  --cov=lib --cov-report=term

# สร้างรายงาน coverage แบบ HTML (เปิดดูรายบรรทัดได้)
python -m pytest tests/ --cov=carekeeper_queue --cov=carekeeper_retry \
  --cov=carekeeper_providers --cov=carekeeper_logging --cov=lib \
  --cov-report=html && open htmlcov/index.html

# แสดงรายชื่อกรณีทดสอบทั้ง 161 รายการ
python -m pytest tests/ --collect-only -q
```

หมายเหตุ: ชุดทดสอบไม่ต้องการไฟล์ `.env`, ฮาร์ดแวร์, หรืออินเทอร์เน็ต — ค่า config ทั้งหมดถูกกำหนดในเทสต์เอง

---

## 7. ข้อจำกัดและแนวทางต่อยอด

1. **การทดสอบกับฮาร์ดแวร์จริง (hardware-in-the-loop)** — การ scan/connect BLE จริง, พอร์ต Serial จริง และคำสั่งระบบบน Raspberry Pi ยังต้องทดสอบด้วยเครื่องจริง แนะนำทำเป็น checklist การทดสอบภาคสนามแยกต่างหาก
2. **Coverage ของ GUI** — `carekeeper_ui.py` มีการทดสอบพฤติกรรมสำคัญ (ปุ่ม disabled, ลำดับ submit) แต่ไม่ได้นับเป็น % coverage เพราะส่วนใหญ่เป็นโค้ดจัดวางหน้าจอ
3. **การทดสอบโหลด/ความทนทานระยะยาว** — เช่น คิวออฟไลน์สะสมหลายพันรายการ หรือรันต่อเนื่องหลายวัน ยังไม่อยู่ในขอบเขตชุดนี้
