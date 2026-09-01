# CareKeeper — เครื่องตรวจสุขภาพเบื้องต้นแบบ Self-service บน Raspberry Pi 5

> **สรุปใน 1 ประโยค:** ตู้ตรวจสุขภาพที่ผู้ใช้เสียบบัตรประชาชนแล้ววัดความดัน / ชีพจร / ออกซิเจนในเลือด / อุณหภูมิ ได้ด้วยตัวเอง แล้วส่งผลขึ้น Backend อัตโนมัติ — ทำงานบน Raspberry Pi 5 แบบ kiosk ที่บูตเองและดูแลตัวเองได้โดยไม่ต้องมีเจ้าหน้าที่ประจำเครื่อง

| | |
|---|---|
| **ภาษา / เฟรมเวิร์ก** | Python, PySide6 (Qt for Python) |
| **ฮาร์ดแวร์** | Raspberry Pi 5 + จอสัมผัส 1010×503 + เซนเซอร์ 5 ชนิด + UPS HAT |
| **ขนาดโค้ด** | ~10,200 บรรทัด (แอป 6 โมดูล + ไลบรารีอุปกรณ์ 5 ชุด + เทสต์ 24 ไฟล์) |
| **ชุดทดสอบ** | **248 test cases ผ่าน 100%** · coverage 70% · รันจบใน ~10 วินาที · ไม่ต้องต่อฮาร์ดแวร์จริง |
| **สถานะ** | ใช้งานได้ครบทั้ง 4 ค่าวัดกับอุปกรณ์จริงแล้ว (end-to-end: บัตรประชาชน → วัด → ส่ง Backend) |

---

## สารบัญ

1. [ปัญหาและเป้าหมายของโครงการ](#1-ปัญหาและเป้าหมายของโครงการ)
2. [ระบบทำอะไรได้บ้าง](#2-ระบบทำอะไรได้บ้าง)
3. [ฮาร์ดแวร์ที่ใช้](#3-ฮาร์ดแวร์ที่ใช้)
4. [Flow การใช้งานจริง](#4-flow-การใช้งานจริง)
5. [สถาปัตยกรรมระบบ](#5-สถาปัตยกรรมระบบ)
6. [จุดเด่นทางวิศวกรรม (หัวข้อสำหรับนำเสนอ)](#6-จุดเด่นทางวิศวกรรม-หัวข้อสำหรับนำเสนอ)
7. [คุณภาพและการทดสอบ](#7-คุณภาพและการทดสอบ)
8. [โครงสร้างไฟล์](#8-โครงสร้างไฟล์)
9. [การติดตั้งและรัน](#9-การติดตั้งและรัน)
10. [การ Deploy แบบ Kiosk บน Raspberry Pi 5](#10-การ-deploy-แบบ-kiosk-บน-raspberry-pi-5)
11. [รูปแบบข้อมูลที่ส่งไป Backend](#11-รูปแบบข้อมูลที่ส่งไป-backend)
12. [ข้อจำกัดปัจจุบันและแผนต่อไป](#12-ข้อจำกัดปัจจุบันและแผนต่อไป)
13. [คำถามที่น่าจะโดนถาม + แนวคำตอบ](#13-คำถามที่น่าจะโดนถาม--แนวคำตอบ)

---

## 1. ปัญหาและเป้าหมายของโครงการ

### ปัญหา

การตรวจสุขภาพเบื้องต้นตามจุดบริการ (รพ.สต., คลินิก, ศูนย์ผู้สูงอายุ) มีข้อจำกัดคือ

- เครื่องวัดแต่ละชนิด **แยกกันคนละเครื่อง** ผู้รับบริการต้องเดินไปทีละจุด และเจ้าหน้าที่ต้องคอยจดค่า
- การ **จดค่าด้วยมือแล้วคีย์เข้าระบบทีหลัง** ทำให้ข้อมูลตกหล่นและผิดพลาด
- ต้องมี **เจ้าหน้าที่ยืนประจำเครื่อง** ตลอดเวลาที่เปิดให้บริการ

### เป้าหมาย

รวมอุปกรณ์วัดทั้งหมดไว้ใน **เครื่องเดียว จอเดียว โปรแกรมเดียว** ที่ผู้ใช้ทั่วไป (รวมถึงผู้สูงอายุ) กดใช้เองได้ และส่งผลเข้าระบบ Backend อัตโนมัติโดยไม่ต้องคีย์ซ้ำ

ข้อจำกัดที่ต้องออกแบบเผื่อไว้ตั้งแต่แรก:

| ข้อจำกัด | ผลต่อการออกแบบ |
|---|---|
| ไม่มีเจ้าหน้าที่ประจำเครื่อง | ระบบต้องกู้ตัวเองได้เมื่ออุปกรณ์หลุด — ห้ามค้างรอคนมากดปิด |
| Wi-Fi ตามจุดบริการไม่เสถียร | ผลตรวจต้อง **ห้ามหาย** แม้เน็ตหลุดตอนกดบันทึก |
| ผู้ใช้เป็นคนทั่วไป | ข้อความ error ต้องบอกว่า *ต้องทำอะไรต่อ* ไม่ใช่บอกว่า *อะไรพัง* |
| จอ 1010×503 แบบสัมผัส | UI ต้องไม่มี scroll, ปุ่มใหญ่, ตัวอักษรใหญ่ |
| เครื่องเปิดทิ้งไว้ยาว ๆ | ต้องกัน memory / log สะสมจนเครื่องช้า |

---

## 2. ระบบทำอะไรได้บ้าง

| ความสามารถ | รายละเอียด |
|---|---|
| อ่านบัตรประชาชนไทย | ดึงเลขบัตร 13 หลัก, ชื่อ-นามสกุล, วันเกิด (แปลง พ.ศ. เป็นข้อความไทย), ที่อยู่ ผ่าน PC/SC + APDU |
| กรอกเลขบัตรเอง | กรณีเครื่องอ่านบัตรมีปัญหา เปิด popup ให้กรอก 13 หลัก พร้อมตรวจว่าเป็นตัวเลขล้วน |
| วัดความดันโลหิต + ชีพจร | SYS / DIA / PULSE จากโมดูลวัดความดัน ผ่าน ESP32 → USB Serial |
| วัดออกซิเจนในเลือด | SpO2 จากเซนเซอร์ MAX30102 (I2C) พร้อมอัลกอริทึม **หาค่านิ่ง** ก่อนคืนค่า |
| วัดอุณหภูมิร่างกาย | โพรบ DS18B20 (1-Wire) ตรวจว่าโพรบแนบผิวจริงและรอค่านิ่ง |
| แสดงสถานะเครื่อง | Wi-Fi / Bluetooth / แบตเตอรี่ (อ่านจาก UPS HAT ผ่าน I2C) / IP address |
| ตั้งค่าเครือข่ายบนเครื่อง | สแกน + เชื่อมต่อ Wi-Fi (รองรับ hidden SSID) และ Bluetooth ได้จากหน้าจอ ไม่ต้องต่อคีย์บอร์ด |
| สรุปผล + ส่ง Backend | รวมผล 4 ค่าในตารางเดียว แล้ว POST เป็น JSON ขึ้น API |
| ดูผลย้อนหลัง | ดึง 4 รายการล่าสุดของผู้รับบริการคนนั้นจาก Backend |
| ทำงานตอนออฟไลน์ | เน็ตหลุด → เก็บผลลง SQLite queue → ส่งเองอัตโนมัติเมื่อกลับมาออนไลน์ |

---

## 3. ฮาร์ดแวร์ที่ใช้

| อุปกรณ์ | เชื่อมต่อผ่าน | ไลบรารีในโปรเจกต์ |
|---|---|---|
| Raspberry Pi 5 (Raspberry Pi OS, Wayland/labwc) | — | — |
| จอสัมผัส 1010×503 | HDMI + USB touch | — |
| เครื่องอ่านบัตรประชาชน | USB (PC/SC daemon `pcscd`) | `lib/thaiidcard/` |
| โมดูลวัดความดัน CN3508 + ESP32 บริดจ์ | UART → USB Serial 115200 | `lib/bp_monitor.py` + firmware `sensor_tests/main.cpp` |
| เซนเซอร์ SpO2 MAX30102 | I2C | `lib/spo2_max30102/` |
| โพรบวัดอุณหภูมิ DS18B20 | 1-Wire (`/sys/bus/w1/`) | `lib/temp_sensor.py` |
| UPS HAT (แบตเตอรี่สำรอง) | I2C (smbus2) | `lib/ups.py` |
| *(เดิม)* นาฬิกา H59 วัด SpO2 | Bluetooth LE (bleak) | `lib/h59_ble/` — ยังเก็บโค้ดไว้ แต่ถูกแทนด้วย MAX30102 แล้ว |

> **จุดที่น่าเล่าตอนนำเสนอ:** ESP32 ไม่ได้เป็นแค่สายแปลง USB — มัน run **state machine** ของตัวเอง (`IDLE → TRIGGER → MEASURING → WAIT_SHUTDOWN → DONE`) เพราะโมดูลวัดความดันต้องถูกกด "สวิตช์" ทางกายภาพเพื่อเริ่มวัด และใช้เวลาอีก ~60 วินาทีกว่าจะ power down เอง รายละเอียดอยู่ใน [หัวข้อ 6.2](#62-เครื่องวัดความดันที่ค้างข้ามการรีสตาร์ต--reset-ก่อนวัดทุกครั้ง)

---

## 4. Flow การใช้งานจริง

```text
  ┌──────────────────────┐
  │ 1. หน้าอ่านบัตรประชาชน │  เสียบบัตร → กดอ่าน
  │                      │  อ่านไม่ได้ → popup กรอกเลขบัตร 13 หลักเอง
  └──────────┬───────────┘
             ▼
  ┌──────────────────────┐   กดวัดทีละอย่าง (ปุ่มบอกสถานะ: กำลังวัด / วัดแล้ว / วัดไม่สำเร็จ)
  │ 2. หน้าวัดสัญญาณชีพ    │   ├─ ความดัน + ชีพจร  (ESP32 → Serial)
  │                      │   ├─ ออกซิเจนในเลือด   (MAX30102 → I2C)
  │                      │   └─ อุณหภูมิ          (DS18B20 → 1-Wire)
  └──────────┬───────────┘
             ▼
  ┌──────────────────────┐   ตารางสรุป 4 ค่า
  │ 3. หน้าสรุปผล         │   ├─ ปุ่ม "ดูข้อมูลย้อนหลัง" → GET 4 รายการล่าสุด
  │                      │   └─ ปุ่ม "บันทึก" → POST ขึ้น Backend
  └──────────┬───────────┘        └─ ส่งไม่สำเร็จ → offline queue → worker ส่งให้เองทีหลัง
             ▼
        กลับหน้าแรก พร้อมรับคนถัดไป
```

### ค่าที่ระบบแสดงผล

| รายการ | รายละเอียด | หน่วย | ช่วงที่ยอมรับ |
|---|---|---|---|
| ความดันโลหิต | SYS / DIA | mmHg | ตามค่าที่โมดูลส่งมา |
| ชีพจร | Pulse | bpm | 30–220 (นอกช่วงถูกปฏิเสธ) |
| ออกซิเจนในเลือด | SpO2 | % | 70–100 (นอกช่วงถูกปฏิเสธ) |
| อุณหภูมิร่างกาย | Body temp | °C | ต้องตรวจพบว่าโพรบแนบผิว |
| ข้อมูลย้อนหลัง | 4 รายการล่าสุด | วันที่/เวลา + ค่าที่วัดได้ | ไม่มีค่าแสดง `-` |
| สถานะอุปกรณ์ | Wi-Fi / Bluetooth / Battery | สถานะ + % | อ่านไม่ได้แสดง `--%` |

---

## 5. สถาปัตยกรรมระบบ

```text
┌───────────────────────────────────────────────────────────────┐
│                    Presentation Layer (UI)                     │
│                       carekeeper_ui.py                         │
│    CareKeeperWindow · ProviderTask(QThread) · Popup · Toast    │
│                    ── ทำงานบน main thread ──                   │
└───────────────┬───────────────────────────────┬───────────────┘
                │ เรียกผ่าน interface            │ Signal/Slot กลับเข้า GUI thread
                ▼                               ▼
┌───────────────────────────────┐   ┌───────────────────────────────┐
│        Provider Layer          │   │       Reliability Layer        │
│    carekeeper_providers.py     │◄──┤     carekeeper_retry.py        │
│   CareKeeperProvider (ABC)     │   │   retry_with_notify()          │
│   ├─ MockCareKeeperProvider    │   │   SubsystemRegistry            │
│   └─ RealCareKeeperProvider    │   │   (auto-disable / auto-enable) │
└───────┬───────────────┬───────┘   └───────────────────────────────┘
        │               │
        ▼               ▼
┌───────────────────┐  ┌─────────────────────────────────┐
│   Hardware I/O     │  │       Backend / Network          │
│  lib/thaiidcard    │  │  requests.post() → API           │
│  lib/bp_monitor    │  │  carekeeper_queue.py             │
│  lib/spo2_max30102 │  │   ├─ SubmissionQueue (SQLite)    │
│  lib/temp_sensor   │  │   └─ QueueDrainWorker (thread)   │
│  lib/ups           │  │                                  │
└───────────────────┘  └─────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│           Cross-cutting: carekeeper_logging.py                 │
│         configure_logging() · log_thread_identity()            │
└───────────────────────────────────────────────────────────────┘
```

### หลักการออกแบบ 4 ข้อ

**1. Dependency Inversion ระหว่าง UI กับ Hardware**

`CareKeeperWindow` ไม่รู้จักฮาร์ดแวร์เลย มันรู้จักแค่ interface `CareKeeperProvider` (`read_patient()`, `measure_spo2()`, `send_data()` …) จึงสลับ Mock ↔ Real ได้โดย **ไม่แก้โค้ด UI แม้แต่บรรทัดเดียว** ต่างกันแค่ entry point:

```python
# main_demo.py                                # main_real.py
run_app(MockCareKeeperProvider(), ...)        run_app(RealCareKeeperProvider(), ...)
```

ผลลัพธ์เชิงปฏิบัติ: ออกแบบ UI บนโน้ตบุ๊กได้โดยไม่ต้องแบก Pi + เซนเซอร์ไปด้วย และเทสต์ UI ทั้งหมดรันได้โดยไม่ต้องมีอุปกรณ์จริง

**2. Non-blocking UI เสมอ**

ทุกงานที่รอ I/O (อ่านบัตร, วัดค่า, ยิง API, สแกน Wi-Fi/BT) ถูกโยนลง `ProviderTask(QThread)` แล้วส่งผลกลับเข้า GUI thread ผ่าน Qt Signal — จอไม่ค้างระหว่างรออุปกรณ์

**3. Resilience by design**

ทุก call ที่แตะฮาร์ดแวร์ห่อด้วย `retry_with_notify()` และทุก payload ที่ส่งไม่สำเร็จตกลง SQLite queue — ไม่ใช่ error handling ที่มาแปะทีหลัง แต่เป็นชั้นที่ออกแบบไว้ตั้งแต่ต้น

**4. Config over code**

ค่าที่เปลี่ยนบ่อย (API URL, API key, serial port, BLE address, threshold ของเซนเซอร์) อ่านจาก `.env` — เปลี่ยนอุปกรณ์หรือย้าย backend ไม่ต้องแก้โค้ด

### Thread Model — ทำไมใช้ thread ทั้งที่ Python มี GIL

Python มี **GIL** ทำให้ thread ไม่ช่วยงาน CPU-bound แต่งานเกือบทั้งหมดของ CareKeeper เป็น **I/O-bound** — รอ serial, รอ I2C, รอ BLE, รอ HTTP — ซึ่งเป็นจังหวะที่ GIL ถูกปล่อยอยู่แล้ว thread จึงเป็นเครื่องมือที่ถูกต้องกับปัญหานี้

| Thread | หน้าที่ |
|---|---|
| Main (GUI) | วาดหน้าจอ, รับ touch, อัปเดต state — ห้าม block เด็ดขาด |
| `ProviderTask` (สร้างชั่วคราวต่อ 1 งาน) | อ่านบัตร / วัดค่า / ส่ง API / สแกน Wi-Fi-BT |
| Status poller | อ่านสถานะ Wi-Fi / Bluetooth / แบตเตอรี่เป็นรอบ |
| `QueueDrainWorker` | poll offline queue ทุก 5 วินาที แล้วส่งของค้างเมื่อออนไลน์ |

การป้องกันเพิ่มอีก 2 ชั้น:

- `network_task` guard — กันผู้ใช้กดสแกน/เชื่อมต่อ Wi-Fi ซ้อนกันหลายครั้ง
- คำสั่งระบบทุกตัว (`nmcli`, `bluetoothctl`, `iwgetid`, `ip link`) กำหนด **timeout** ทั้งหมด กัน subprocess ค้างแล้วลาก thread ค้างตาม

> ถ้าอนาคตมีงาน CPU-bound จริง (เช่น ประมวลผลสัญญาณ ECG แบบ real-time) ควรแยกเป็น process/service ต่างหาก ไม่ใช่เพิ่ม thread

---

## 6. จุดเด่นทางวิศวกรรม (หัวข้อสำหรับนำเสนอ)

หัวข้อในส่วนนี้คือ "ปัญหาจริงที่เจอหน้างาน แล้วแก้ยังไง" — เหมาะกับการเล่าในสัมภาษณ์มากกว่าการไล่ feature

### 6.1 SpO2 ที่ "นิ่งแล้วค่อยตอบ" ไม่ใช่ค่าแรกที่อ่านได้

**ปัญหา:** MAX30102 คืนค่า SpO2 ออกมาเรื่อย ๆ ทุกหน้าต่างสัญญาณ แต่ค่าแรก ๆ มักเพี้ยน (เช่น 90% ทั้งที่คนปกติ) เพราะนิ้วยังวางไม่นิ่ง ถ้าเอาค่าแรกที่อยู่ในช่วง 70–100 ไปแสดง ผู้ใช้จะเห็นค่าที่ผิด

**วิธีแก้:** เพิ่มชั้น "หาค่านิ่ง" ใน `lib/spo2_max30102/spo2_monitor.py` — เก็บค่าล่าสุดเป็นหน้าต่างเลื่อน (sliding window 5 ค่า) แล้วจะคืนค่าก็ต่อเมื่อ **สเปรดของหน้าต่างนั้น ≤ threshold** จากนั้นคืน **ค่ามัธยฐาน** ของหน้าต่าง ไม่ใช่ค่าล่าสุด

เพิ่มการป้องกันอีก 3 อย่าง:
- ตรวจ **IR DC level** ว่านิ้วอยู่บนเซนเซอร์จริงไหม (`FINGER_IR_THRESHOLD`)
- ถ้าผู้ใช้ **ยกนิ้วออกกลางคัน** → ล้างหน้าต่างทิ้งทั้งชุด เริ่มนับใหม่ ไม่ผสมค่าก่อน/หลังยกนิ้ว
- แยกสาเหตุความล้มเหลวเป็น 3 แบบ (`NO_FINGER` / `WEAK_SIGNAL` / `UNSTABLE`) เพราะแต่ละแบบผู้ใช้ต้องทำคนละอย่าง — และแนบค่า IR จริงกับ threshold ในข้อความ error เพื่อให้ตั้งค่า threshold ใหม่ได้จากหน้างาน

**ทดสอบไว้ 23 เคส** เช่น TC-24 (ป้อนลำดับ 90, 99, 95, 97, 97, 97, 97 → ต้องไม่คืน 90 แต่คืน 97), TC-25 (ยกนิ้วกลางคัน → ต้องไม่ผสมค่า), TC-26 (ค่า 40% / 101% ต้องถูกทิ้ง)

### 6.2 เครื่องวัดความดันที่ค้างข้ามการรีสตาร์ต — RESET ก่อนวัดทุกครั้ง

**ปัญหา:** โมดูล CN3508 หลังวัดเสร็จจะยังไม่ปิดตัวทันที ใช้เวลาอีก ~60 วินาทีถึงจะส่งสัญญาณ power down ถ้าโปรแกรมถูกปิด/restart ระหว่างช่วงนี้ firmware บน ESP32 จะยัง **ค้างอยู่ใน state เดิม** แล้วตอบ `NOT_READY` ทุกครั้งที่สั่งวัด — อาการที่ผู้ใช้เห็นคือ "เปิดเครื่องมาแล้ววัดความดันไม่ติดเลย ต้องถอดสาย USB เสียบใหม่"

**วิธีแก้ 3 ชั้น:**

| ชั้น | สิ่งที่ทำ |
|---|---|
| Firmware (`sensor_tests/main.cpp`) | เพิ่มคำสั่ง `RESET` ที่บังคับกลับ `IDLE` + ปล่อยสวิตช์ทริกเกอร์ และเพิ่ม **timeout ราย state** (120 วิ) ให้ firmware ปลดตัวเองได้แม้สายหลุด/ไฟตกกลางคัน |
| Driver (`lib/bp_monitor.py`) | ตอน `connect()` จะส่ง `RESET` แล้วรอ `READY` เสมอ ล้าง state ค้างก่อนเริ่มวัดทุกครั้ง |
| Protocol | firmware ตอบ `NOT_READY:<state>` (เช่น `NOT_READY:MEASURING`) ทำให้แอปแยกได้ว่า "รอสักครู่" กับ "รอเป็นนาที" แล้วเลือกข้อความที่ถูกต้องให้ผู้ใช้ |

**เสริมที่ฝั่ง UI:** ปุ่มวัดความดันมี **cooldown 60 วินาที** หลังวัดสำเร็จหรือเจอ device error กันผู้ใช้กดรัว ๆ ตอนที่โมดูลยังไม่พร้อม (ทดสอบไว้ 9 เคส)

### 6.3 Serial port ที่เลขเปลี่ยนไปเรื่อย — auto-detect โดยไม่แตะพอร์ต

**ปัญหา:** `/dev/ttyUSB0` กลายเป็น `/dev/ttyUSB1` ได้ทุกครั้งที่เสียบสายใหม่หรือบูตใหม่ ถ้า hardcode ไว้ใน `.env` เครื่องจะพังทันทีที่เลขเปลี่ยน

**วิธีแก้:** `_resolve_bp_port()` ไล่ลำดับความสำคัญคือ ค่าใน `.env` ที่ยังเสียบอยู่จริง → พอร์ต USB-serial ที่ชิปบริดจ์ตรงกับรายการที่รู้จัก (CH34x, CP210x, FTDI, Prolific, ESP32 native USB) → พอร์ต USB-serial ตัวเดียวที่มี

**จุดสำคัญด้านความปลอดภัย:** ฟังก์ชันนี้ **อ่านอย่างเดียว ไม่เปิดพอร์ตและไม่เขียนอะไรลงไป** — เพราะถ้าไป probe ผิดพอร์ตที่เป็นเครื่องวัดความดันจริง มันอาจ **สั่งให้ผ้าพันแขนบีบจริง** ใส่คนที่ยังไม่พร้อม

### 6.4 Retry + Auto-disable subsystem

อุปกรณ์ที่ต่อผ่าน serial / I2C / BLE หลุดได้บ่อย ระบบจึงห่อทุก call ด้วย `retry_with_notify()`:

- ลองซ้ำสูงสุด 3 ครั้ง แบบ linear backoff
- `SubsystemRegistry` (process-wide singleton) เก็บสถานะรายอุปกรณ์: `wifi`, `bluetooth`, `idcard`, `bp_monitor`, `spo2`
- ครบ 3 ครั้งยังไม่สำเร็จ → `disable()` อุปกรณ์นั้น แล้ว UI อัปเดตปุ่ม/สถานะให้ผู้ใช้เห็นว่าใช้ไม่ได้ชั่วคราว
- ผู้ใช้กดใช้อีกครั้ง → `enable()` กลับมาลองใหม่อัตโนมัติ ไม่ต้องรีสตาร์ตโปรแกรม

**การตัดสินใจออกแบบที่น่าเล่า:** สำหรับ SpO2 และความดัน เราแยก **"เปิดอุปกรณ์ไม่ได้"** (= ฮาร์ดแวร์พังจริง → retry แล้ว disable) ออกจาก **"วัดไม่สำเร็จ"** (= ผู้ใช้ยังไม่วางนิ้ว → ไม่ควร disable อุปกรณ์) เพราะถ้าไม่แยก คนที่วางนิ้วช้าจะทำให้เซนเซอร์ถูกปิดทั้งที่มันปกติดี

### 6.5 Offline Queue — ผลตรวจต้องไม่หายแม้เน็ตหลุด

`SubmissionQueue` เป็นคิว FIFO เก็บลง SQLite (`data/carekeeper_queue.db`):

- ส่ง Backend ไม่สำเร็จ → `enqueue()` ลงไฟล์แทนการทิ้ง จึงรอดจากการปิด/รีสตาร์ตโปรแกรมและไฟดับ
- `QueueDrainWorker` เป็น background thread poll ทุก 5 วินาที ถ้ากลับมาออนไลน์จะส่งของค้างตามลำดับเดิม ส่งสำเร็จลบทิ้ง ส่งไม่สำเร็จเพิ่ม `attempts` แล้วลองใหม่
- ถ้าโปรแกรมถูกปิดตอนสถานะเป็น `sending` → รอบเปิดครั้งถัดไปจะ reset กลับเป็น `pending` กันข้อมูลค้างแบบเงียบ ๆ

### 6.6 Wi-Fi hidden SSID ที่ nmcli เชื่อมไม่ได้ตรง ๆ

**ปัญหา:** จุดบริการหลายที่ใช้ Wi-Fi ที่ซ่อนชื่อ `nmcli` จะ fail ด้วยข้อความ `802-11-wireless-security.key-mgmt: property is missing` ซึ่งไม่ได้บอกเลยว่าปัญหาคือ SSID ซ่อนอยู่

**วิธีแก้:** ตรวจจับ error string นั้นโดยเฉพาะ แล้ว retry อัตโนมัติด้วย `hidden yes` ภายในการกดครั้งเดียวของผู้ใช้ — ผู้ใช้ไม่ต้องรู้ว่ามันเป็น hidden network (ทดสอบไว้เป็น TC-23)

นอกจากนี้ยังมี rollback: ถ้าเชื่อมเน็ตใหม่ไม่สำเร็จ ระบบจะ **ลบ profile ที่สร้างค้างไว้ทิ้ง แล้วกลับไปต่อเน็ตเดิม** ไม่ทิ้งเครื่องไว้ในสภาพไม่มีเน็ต

### 6.7 งานฝั่ง OS — ทำให้ Pi เป็นตู้ kiosk จริง ๆ

โค้ดแอปอย่างเดียวไม่พอ ต้องปรับ Raspberry Pi OS ให้บูตขึ้นมาแล้วเข้าโปรแกรมเลย

| งาน | วิธีทำ | ไฟล์อ้างอิง |
|---|---|---|
| Auto-run ตอนบูต | systemd service รัน `main_real.py` เมื่อเข้า graphical target, รอ `pcscd` พร้อมก่อน, ตั้ง `Restart=on-failure` ให้ฟื้นเองเมื่อ crash | `conf/carekeeper.service` |
| Restart ทุกคืน | timer สั่ง restart ตี 3 ล้าง memory/cache ที่สะสมจากการรันต่อเนื่อง | `conf/carekeeper-restart.{service,timer}` |
| จำกัดขนาด log | `systemd-journald` ใช้ได้ไม่เกิน 200MB / เก็บย้อนหลัง 14 วัน กัน disk เต็ม | `conf/carekeeper-journal.conf` |
| สิทธิ์เข้าถึง USB sensor | udev rule ให้เข้าถึงได้โดยไม่ต้องเป็น root | `/etc/udev/rules.d/99-thaiidcard.rules` (บนเครื่อง) |
| อ่านบัตรได้แม้ไม่มีคน login | polkit rule ให้ `pcscd` อนุญาต client ที่ไม่มี active session — เพราะ kiosk service ไม่ผูกกับ session ผู้ใช้ | `/etc/polkit-1/rules.d/50-pcscd.rules` (บนเครื่อง) |
| ซ่อน desktop/taskbar | แก้ autostart ของ labwc (Wayland compositor) | `/etc/xdg/labwc/autostart` (บนเครื่อง) |
| ซ่อนข้อความตอนบูต | แทน splash ของ Plymouth ด้วยพื้นสีเดียวกับพื้นหลัง | `/usr/share/plymouth/themes/pix/splash.png` (บนเครื่อง) |

ขั้นตอนเต็มอยู่ใน [`docs/carekeeper-pi5-setup-runbook.md`](docs/carekeeper-pi5-setup-runbook.md)

### 6.8 Error message ที่เขียนให้คนทั่วไปอ่าน

หลักคือ error ต้องบอกว่า **ต้องทำอะไรต่อ** ไม่ใช่บอกว่าอะไรพัง

| กรณี | สิ่งที่ผู้ใช้เห็น |
|---|---|
| ไม่พบนิ้วบนเซนเซอร์ | "ไม่พบนิ้วบนเซนเซอร์ SpO2 (วางนิ้วให้แนบเต็มหน้าเซนเซอร์)" |
| ค่ายังไม่นิ่ง | "ค่า SpO2 ยังไม่นิ่ง (อยู่นิ่งๆ อย่าขยับนิ้วระหว่างวัด)" |
| โมดูลความดันยังไม่ว่าง | แยกข้อความตาม state จริงของ firmware ว่าให้รอสักครู่ หรือรอเป็นนาที |
| อ่านแบตเตอรี่ไม่ได้ | แสดง `--%` ไม่ใช่ `0%` — เพราะ `0%` ทำให้เข้าใจผิดว่าแบตหมด |
| ส่ง Backend ไม่สำเร็จ | บอกว่าเก็บไว้แล้วและจะส่งให้อัตโนมัติ ไม่ใช่ให้ผู้ใช้ตกใจว่าข้อมูลหาย |

Toast แจ้งเตือนแสดงกลางจอ (ไม่ทับ footer) และการแจ้งเตือนจาก status poller / queue worker ถูก **จำกัดความถี่** ไม่ให้เด้งซ้ำ ๆ ระหว่างใช้งาน

---

## 7. คุณภาพและการทดสอบ

| ตัวชี้วัด | ค่า |
|---|---|
| จำนวน test cases | **248** |
| ผ่าน | **248 (100%)** |
| Coverage (โมดูลตรรกะ) | **70%** (1,797 statements) |
| เวลารันทั้งชุด | ~10 วินาที |
| ต้องใช้ฮาร์ดแวร์/เน็ตจริงไหม | **ไม่ต้อง** — deterministic รันซ้ำได้ทุกเครื่อง |

พัฒนาการ: เริ่มจาก 52 เคส (coverage 57%) → 161 เคส (79%) → **248 เคส (70%)** — ตัวเลข % ลดลงเพราะรวมไลบรารีเซนเซอร์ที่ย้ายเข้ามาใหม่ (MAX30102, DS18B20) ซึ่งส่วนใหญ่เป็นโค้ดคุยฮาร์ดแวร์โดยตรง ไม่ใช่เพราะเทสต์ลดลง

### กลยุทธ์: แทนฮาร์ดแวร์ด้วย test double

| ของจริง | ตัวแทนในเทสต์ |
|---|---|
| เครื่องวัดความดัน (ESP32/Serial) | ป้อนบรรทัดโปรโตคอลตรงเข้า parser |
| นาฬิกา H59 (BLE) | อุปกรณ์จำลองที่บันทึกคำสั่งและฉีดแพ็กเก็ตตอบกลับ |
| เครื่องอ่านบัตร (PC/SC) | connection จำลองที่คืน APDU ตามสคริปต์ |
| UPS HAT (I2C) | โมดูล `smbus` ปลอมที่ตั้งค่า register ได้ |
| MAX30102 (I2C) | เซนเซอร์ปลอมที่ป้อนบล็อกสัญญาณ + สคริปต์ค่าที่อัลกอริทึมคืน |
| Backend API (HTTP) | `requests.get/post` ปลอมที่บันทึกทุก call |

### ตัวอย่างเคสที่วัดผลได้ชัด

| รหัส | ทดสอบอะไร | ป้อน | คาดหวัง |
|---|---|---|---|
| TC-10 | แปลงผลเครื่องวัดความดัน | `"SYS:120,DIA:80,PUL:72"` | `BPResult(120, 80, 72)` |
| TC-11 | ข้อความผิดรูปแบบไม่ทำให้พัง | `"hello world"`, ค่าไม่ครบ | คืน `None` ทุกกรณี |
| TC-12 | วันเกิดบัตร (พ.ศ.) | `"25320415"` | `"15 เมษายน 2532"` |
| TC-16 | กระแสไฟติดลบตอนจ่ายไฟ | ไบต์ `[0x0C, 0xFE]` | −499 mA (two's complement) |
| TC-22 | retry แล้ว disable | ล้มเหลว 3 ครั้งติด | subsystem ถูก disable พร้อมเหตุผล |
| TC-24 | SpO2 ต้องนิ่งก่อนคืนค่า | 90, 99, 95, 97, 97, 97, 97 | ไม่คืน 90 → คืน 97 |

รายงานฉบับเต็ม: [`docs/test-report.md`](docs/test-report.md)

### รันเทสต์เอง

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

วัด coverage:

```bash
python -m pytest tests/ --cov=carekeeper_queue --cov=carekeeper_retry --cov=carekeeper_providers --cov=carekeeper_logging --cov=lib --cov-report=term
```

> บน Linux/CI ที่ไม่มีจอ ให้ตั้ง `QT_QPA_PLATFORM=offscreen` ก่อนรัน เพื่อให้เทสต์ GUI ทำงานได้

---

## 8. โครงสร้างไฟล์

```text
Care_Keeper/
├── main_demo.py               # entry point: รัน GUI ด้วยข้อมูลจำลอง
├── main_real.py               # entry point: รัน GUI กับอุปกรณ์จริงบน Pi
│
├── carekeeper_ui.py           # ชั้น UI ทั้งหมด: หน้าจอ, event, popup, toast, ProviderTask
├── carekeeper_providers.py    # ชั้น provider: interface + Mock + Real (คุยฮาร์ดแวร์/Backend)
├── carekeeper_retry.py        # ชั้น reliability: retry + auto-disable subsystem
├── carekeeper_queue.py        # ชั้น offline: SQLite queue + background drain worker
├── carekeeper_logging.py      # logging กลาง + log thread identity สำหรับ debug
├── carekeeper_style.py        # stylesheet: สี ฟอนต์ รูปแบบองค์ประกอบ
│
├── lib/                       # ไดรเวอร์อุปกรณ์จริง
│   ├── thaiidcard/            #   บัตรประชาชน (PC/SC, APDU, TIS-620, แปลง พ.ศ.)
│   ├── bp_monitor.py          #   เครื่องวัดความดัน (Serial + โปรโตคอล + RESET)
│   ├── spo2_max30102/         #   SpO2 (I2C + อัลกอริทึมหาค่านิ่ง)
│   ├── temp_sensor.py         #   อุณหภูมิ DS18B20 (1-Wire)
│   ├── ups.py                 #   แบตเตอรี่ UPS HAT (I2C)
│   └── h59_ble/               #   (legacy) SpO2 ผ่าน Bluetooth LE
│
├── sensor_tests/              # สคริปต์ทดสอบอุปกรณ์แยกเดี่ยว ก่อนเปิด GUI จริง
│   ├── idcard.py  BP.py  H59_BLE.py  battery.py  ble_scaner.py
│   └── main.cpp               #   firmware ESP32 ของเครื่องวัดความดัน
│
├── tests/                     # pytest 248 เคส (+ fakes/ สำหรับแทนฮาร์ดแวร์)
├── conf/                      # systemd service/timer + journald config สำหรับ kiosk
├── docs/                      # เอกสารฉบับเต็ม, runbook ติดตั้ง Pi 5, รายงานผลทดสอบ
├── style/                     # ฟอนต์ไทย/อังกฤษ และไอคอน
└── data/                      # SQLite ของ offline queue (สร้างตอนรัน ไม่ commit)
```

---

## 9. การติดตั้งและรัน

### 9.1 Python environment

```bash
python -m venv .venv
```

เปิดใช้งาน (Raspberry Pi / Linux / macOS):

```bash
source .venv/bin/activate
```

เปิดใช้งาน (Windows PowerShell):

```powershell
.\.venv\Scripts\Activate.ps1
```

ติดตั้ง dependencies:

```bash
pip install -r requirement.txt
```

Dependencies หลัก: `PySide6` (GUI), `pyserial` (เครื่องวัดความดัน), `pyscard` (บัตรประชาชน), `smbus2` (I2C: SpO2 + UPS), `bleak` (BLE), `requests` (Backend), `python-dotenv` (config), `numpy`, `pyqtgraph`

### 9.2 System packages (Raspberry Pi)

```bash
sudo apt update && sudo apt install -y python3-smbus i2c-tools bluetooth bluez pcscd libpcsclite-dev swig python3-dev
```

เพิ่มสิทธิ์เข้าถึงอุปกรณ์แล้ว reboot:

```bash
sudo usermod -aG dialout,bluetooth,i2c $USER && sudo reboot
```

### 9.3 ตั้งค่า `.env`

สร้างไฟล์ `.env` ที่ root ของโปรเจกต์ (ไฟล์นี้อยู่ใน `.gitignore` แล้ว — **ห้าม commit**)

```env
# Backend API
CAREKEEPER_API_URL=https://<backend-host>/api/v2/device/add_health
CAREKEEPER_API_KEY_HEADER=api-key
CAREKEEPER_API_KEY=<your-api-key>

# Measurement history API
CAREKEEPER_HISTORY_API_URL=https://<backend-host>/api/v2/device/health_history
CAREKEEPER_HISTORY_PATIENT_ID_PARAM=patient_id
CAREKEEPER_HISTORY_MAC_PARAM=mac

# Device configuration
CAREKEEPER_BP_PORT=auto            # หรือระบุพอร์ตตรง ๆ เช่น /dev/ttyUSB0
CAREKEEPER_H59_DEVICE_NAME=<ble-device-name>
CAREKEEPER_H59_DEVICE_ADDRESS=<ble-mac-address>
```

> MAC address ของเครื่องไม่ต้องตั้งค่า — ระบบอ่านจาก Raspberry Pi เองอัตโนมัติ

### 9.4 รันโปรแกรม

โหมดจำลอง (ไม่ต้องมีอุปกรณ์ — ใช้ทดสอบ UI และตอนนำเสนอ):

```bash
python main_demo.py
```

โหมดอุปกรณ์จริง (บน Raspberry Pi):

```bash
python main_real.py
```

### 9.5 ทดสอบอุปกรณ์แยกทีละตัว

รันจาก root ของโปรเจกต์เสมอ เพื่อให้ path ของ `lib/` ถูกต้อง:

```bash
python sensor_tests/idcard.py
```

```bash
python sensor_tests/BP.py
```

```bash
python sensor_tests/battery.py
```

การแยกทดสอบช่วยระบุปัญหาได้เร็ว เช่น พอร์ต serial ผิด, สิทธิ์ผู้ใช้ไม่พอ, หรือ driver เครื่องอ่านบัตรไม่ทำงาน

---

## 10. การ Deploy แบบ Kiosk บน Raspberry Pi 5

```bash
sudo cp conf/carekeeper.service /etc/systemd/system/
```

```bash
sudo cp conf/carekeeper-restart.service conf/carekeeper-restart.timer /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now carekeeper.service carekeeper-restart.timer
```

จำกัดขนาด log:

```bash
sudo cp conf/carekeeper-journal.conf /etc/systemd/journald.conf.d/ && sudo systemctl restart systemd-journald
```

ดู log ของเครื่อง:

```bash
journalctl -u carekeeper.service -f
```

ขั้นตอนเต็มตั้งแต่ `raspi-config`, udev, polkit, labwc autostart, ไปจนถึง checklist ตรวจหลัง reboot อยู่ใน [`docs/carekeeper-pi5-setup-runbook.md`](docs/carekeeper-pi5-setup-runbook.md)

---

## 11. รูปแบบข้อมูลที่ส่งไป Backend

`POST` พร้อม header `api-key` (ชื่อ header ตั้งได้จาก `.env`)

```json
{
  "mac": "1c:ce:51:9a:34:77",
  "spo2": 98,
  "heart_rate": 70,
  "pr_bpm": 70,
  "sys": 120,
  "dia": 78,
  "pulse": 70
}
```

- `mac` อ่านจาก Raspberry Pi อัตโนมัติ ใช้ระบุว่าข้อมูลมาจากเครื่องไหน
- `heart_rate`, `pr_bpm`, `pulse` ใช้ค่าชีพจรชุดเดียวกันตามรูปแบบ API ปัจจุบัน
- HTTP 2xx = สำเร็จ · 401/500 = โยน error แล้วเข้า offline queue

ดึงข้อมูลย้อนหลัง: `GET` พร้อม `patient_id`, `mac`, `limit=4` — ฝั่งแอปรองรับหลายรูปแบบ response (`data` / `records` / `history` / array ตรง ๆ) และ map ชื่อฟิลด์ให้ยืดหยุ่น (`sys`/`systolic`, `pulse`/`pr_bpm`/`heart_rate`) เพื่อไม่ให้พังเมื่อ Backend ปรับ schema เล็กน้อย

---

## 12. ข้อจำกัดปัจจุบันและแผนต่อไป

| # | เรื่อง | สถานะ / แผน |
|---|---|---|
| 1 | Log ยังออกที่ console เป็นหลัก | `carekeeper_logging.py` ตั้งค่าไว้แล้ว แต่ควรต่อเข้า `journald` หรือเขียนไฟล์แบบ rotate ให้ตรวจย้อนหลังได้ดีขึ้น |
| 2 | Mock กับ Real ใช้ queue path เดียวกัน | ถ้าเปิด `main_demo.py` ขณะมีข้อมูลจริงค้างในคิว worker ของ mock อาจ drain ทิ้ง — ควรแยก path ของ mock/real |
| 3 | ความเสี่ยงส่ง payload ซ้ำ | ตอนกดบันทึกจะ enqueue แล้วส่งทันที ขณะที่ worker ก็อาจเห็น row เดียวกัน — production ควรให้ทุกการส่งผ่าน worker ทางเดียว |
| 4 | ยังไม่มี hardware-in-the-loop test | การ scan/connect BLE จริง, serial จริง, คำสั่ง OS จริง ยังต้องทดสอบด้วยเครื่อง — ควรทำเป็น checklist ภาคสนาม |
| 5 | ยังไม่มี load test ระยะยาว | เช่น คิวสะสมหลายพันรายการ หรือรันต่อเนื่องหลายวัน |
| 6 | ยังไม่มีไฟล์ `.env.example` ใน repo | `.gitignore` เปิดช่องให้ commit ได้แล้ว (`!.env.example`) แต่ยังไม่ได้สร้างไฟล์ |
| 7 | โค้ด H59 BLE ยังค้างอยู่ | ถูกแทนที่ด้วย MAX30102 แล้ว ควรตัดสินใจว่าจะเก็บเป็น fallback หรือถอดออก |

---

## 13. คำถามที่น่าจะโดนถาม + แนวคำตอบ

**Q: ทำไมเลือก PySide6 ไม่ใช้ web (Flask/React) หรือ Tkinter?**
เครื่องนี้เป็น kiosk ออฟไลน์ได้ ไม่ควรพึ่ง browser + web server บน Pi ซึ่งกิน RAM และเพิ่มจุดพัง ส่วน Tkinter ทำ UI แบบที่ต้องการ (ฟอนต์ไทย, ปุ่มใหญ่, styling, thread integration) ได้ลำบากกว่ามาก PySide6 ให้ Qt Signal/Slot ที่จัดการ cross-thread ได้ปลอดภัยในตัว ซึ่งเป็นหัวใจของแอปนี้ และรันเป็น native app บน Wayland ได้ตรง ๆ

**Q: Python มี GIL แล้วใช้ thread ทำไม?**
งานทั้งหมดที่โยนเข้า thread เป็น I/O-bound (รอ serial / I2C / BLE / HTTP) ซึ่ง GIL ถูกปล่อยระหว่างรออยู่แล้ว thread จึงได้ประโยชน์เต็มที่ ถ้าเป็นงาน CPU-bound เช่นประมวลผลสัญญาณ ผมจะแยกเป็น process ต่างหาก ไม่ใช่เพิ่ม thread

**Q: ทดสอบยังไงในเมื่อต้องมีฮาร์ดแวร์?**
แยกชั้น provider ออกจาก UI แล้วแทนฮาร์ดแวร์ด้วย fake ที่ระดับ boundary — เช่นเทสต์ parser ของเครื่องวัดความดันด้วยการป้อนบรรทัดโปรโตคอลตรง ๆ เทสต์ UPS ด้วย `smbus` ปลอมที่ตั้งค่า register ได้ ผลคือ 248 เคสรันจบใน ~10 วินาทีบนเครื่องไหนก็ได้ ไม่ต้องต่ออุปกรณ์เลย

**Q: ปัญหาที่ยากที่สุดคืออะไร?**
เครื่องวัดความดันวัดไม่ติดหลัง restart — อาการมันชี้ไปที่ซอฟต์แวร์ แต่ต้นเหตุจริงอยู่ที่ firmware ค้าง state ข้ามการรีสตาร์ตของฝั่ง Pi ต้องแก้ทั้ง 3 ชั้น: เพิ่มคำสั่ง `RESET` + timeout ใน firmware, ส่ง `RESET` ทุกครั้งตอน connect ในไดรเวอร์, และเพิ่ม `NOT_READY:<state>` ในโปรโตคอลเพื่อให้แอปบอกผู้ใช้ได้ถูกว่าต้องรอนานแค่ไหน

**Q: ถ้าเน็ตหลุดตอนกดบันทึกจะเกิดอะไรขึ้น?**
ข้อมูลไม่หาย — payload ถูกเขียนลง SQLite ก่อน ถ้าส่งไม่สำเร็จมันจะค้างอยู่ในคิวข้ามการปิดเครื่อง แล้ว background worker จะส่งให้เองเมื่อกลับมาออนไลน์ ผู้ใช้เห็นข้อความว่าเก็บไว้แล้ว ไม่ใช่ข้อความว่าล้มเหลว

**Q: ทำไม SpO2 ต้องรอค่านิ่ง ทำไมไม่คืนค่าแรกที่อ่านได้?**
เพราะค่าแรก ๆ จากอัลกอริทึมมักเพี้ยนตอนนิ้วยังไม่นิ่ง ถ้าคืนไปเลยผู้ใช้จะเห็นค่าที่ผิดโดยไม่รู้ตัว ระบบจึงรอจนค่าใน sliding window 5 ค่าเกาะกลุ่มกันแล้วคืนค่ามัธยฐาน และถ้ายกนิ้วออกกลางคันจะล้างหน้าต่างเริ่มใหม่ ไม่ผสมค่าคนละช่วง

**Q: ถ้าอุปกรณ์ตัวหนึ่งพัง ระบบยังใช้งานได้ไหม?**
ได้ — `SubsystemRegistry` จะ disable เฉพาะอุปกรณ์ที่ retry ครบแล้วยังไม่สำเร็จ ส่วนที่เหลือยังวัดและส่งข้อมูลได้ตามปกติ และเมื่อผู้ใช้กดใช้อุปกรณ์นั้นอีกครั้ง ระบบจะลองใหม่อัตโนมัติโดยไม่ต้องรีสตาร์ตโปรแกรม

**Q: ความปลอดภัยของข้อมูลผู้ป่วย?**
API key และ endpoint อยู่ใน `.env` ที่ไม่ commit เข้า git, ส่งผ่าน HTTPS, และข้อมูลที่ค้างในคิวเป็นค่าวัดกับเลขอ้างอิงเท่านั้น สิ่งที่ควรทำเพิ่มถ้าขึ้น production จริงคือเข้ารหัสไฟล์คิวและกำหนดอายุข้อมูลที่เก็บบนเครื่อง

**Q: ถ้าจะเพิ่มเซนเซอร์ตัวใหม่ต้องแก้ตรงไหน?**
เพิ่มไดรเวอร์ใน `lib/`, เพิ่มเมธอดใน interface `CareKeeperProvider`, แล้ว implement ทั้งใน Mock และ Real — UI แตะเฉพาะส่วนแสดงผล ไม่ต้องรู้ว่าอุปกรณ์คุยกันด้วยโปรโตคอลอะไร การเปลี่ยน SpO2 จาก H59 (BLE) มาเป็น MAX30102 (I2C) ทำได้โดยแก้แค่ในชั้น provider เป็นตัวอย่างว่ามันได้ผลจริง

---

## เอกสารเพิ่มเติมในโปรเจกต์

| เอกสาร | เนื้อหา |
|---|---|
| [`docs/CareKeeper_Full_Documentation.md`](docs/CareKeeper_Full_Documentation.md) | เอกสารฉบับสมบูรณ์: อ้างอิงฟังก์ชันรายโมดูล, data flow, ตัวแปรแวดล้อม |
| [`docs/carekeeper-pi5-setup-runbook.md`](docs/carekeeper-pi5-setup-runbook.md) | ขั้นตอน setup เครื่อง Pi 5 ใหม่ตั้งแต่ต้นจนจบ |
| [`docs/test-report.md`](docs/test-report.md) | รายงานผลทดสอบซอฟต์แวร์ 248 เคส พร้อมตาราง coverage |
| [`lib/h59_ble/README.md`](lib/h59_ble/README.md) | โปรโตคอล BLE ของนาฬิกา H59 (legacy) |
