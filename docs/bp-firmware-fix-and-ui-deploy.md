# CareKeeper — บันทึกการแก้ BP และวิธีอัปเดต UI ไป Raspberry Pi

วันที่ทดสอบ: 1 กันยายน 2026  
เครื่องปลายทาง: `raspberry@10.186.252.29:/home/raspberry/HealthLink`

## 1. อาการเดิม

เมื่อเรียก `python bp_monitor.py /dev/ttyUSB0` โปรแกรมเปิด Serial ได้ แต่ ESP32 ไม่ตอบคำสั่ง `RESET`:

```text
[BPMonitor] Connected to /dev/ttyUSB0 -- no answer to RESET in 3s
[BPMonitor] NOT_READY (firmware state: unknown)
```

บางรอบเริ่มวัดได้ แต่จบด้วย `BP_ERROR` โดยไม่มีผล SYS/DIA/PUL

## 2. สาเหตุ

- Python driver บน Pi เป็นรุ่นใหม่และส่งคำสั่ง `RESET` ก่อนเริ่มวัด
- Firmware เดิมใน ESP32 ยังเป็นรุ่นเก่า จึงไม่รู้จัก `RESET`, `STATUS` และยังตอบ `NOT_READY` โดยไม่บอก state
- `/dev/ttyUSB0` และสิทธิ์ Serial ไม่ได้เสีย เพราะ ESP32 ยังตอบ `START`, `NOT_READY` และ `BP_ERROR` กลับมาได้

## 3. สิ่งที่ดำเนินการ

1. สำรอง Flash เดิมของ ESP32 ไว้ที่:

   ```text
   /home/raspberry/HealthLink/esp32-original-backup.bin
   ```

2. ใช้ `esptool flash-id` ตรวจว่าพอร์ต `/dev/ttyUSB0` เข้า bootloader อัตโนมัติผ่าน DTR/RTS ได้โดยไม่ต้องเปิดกล่องหรือกดปุ่ม BOOT
3. ตรวจพบชิป `ESP32-D0WDQ6`, crystal 40 MHz และ Flash 4 MB
4. สร้าง PlatformIO project ด้วย board ID `esp32dev`
5. Build และ upload firmware ปัจจุบันจาก `sensor_tests/main.cpp` ผ่าน `/dev/ttyUSB0`
6. ตรวจ protocol หลังแฟลช:

   ```text
   RESET  -> READY
   STATUS -> STATE:IDLE
   ```

7. ทดสอบวัดจริงสำเร็จ:

   ```text
   SYS: 112 mmHg
   DIA:  66 mmHg
   PUL:  72 bpm
   เวลา: 47.9 วินาที
   ```

## 4. ผลหลังแก้

- ESP32 ตอบ `RESET` และพร้อมเริ่มรอบใหม่ได้
- Firmware รายงาน state ได้ เช่น `IDLE`, `MEASURING` และ `WAIT_SHUTDOWN`
- ลดปัญหา state ค้างหลังปิด/เปิดแอป
- Pi, ESP32, CN3508 และ parser ใน `bp_monitor.py` ส่งข้อมูลครบทั้งเส้นทางแล้ว
- `BP_ERROR` อาจยังเกิดเป็นครั้งคราวจาก cuff, สายลม, การขยับแขน หรือเงื่อนไขการวัด จึงควรเว้นรอบวัดประมาณ 2 นาที

## 5. อัปเดต UI จาก Windows ไป Pi ด้วย SCP

คำสั่งต่อไปนี้รันใน Windows PowerShell จากโฟลเดอร์:

```text
C:\Users\User\Desktop\Project\Care_Keeper
```

### 5.1 ปิดโปรแกรมบน Pi ก่อน

ถ้ารันใน terminal ให้กด `Ctrl+C` ก่อน หากไม่แน่ใจ ตรวจ process ด้วย:

```bash
pgrep -af main_real.py
```

### 5.2 สำรอง UI เดิมบน Pi

รันบน Pi:

```bash
mkdir -p ~/HealthLink-backup-20260901
cp ~/HealthLink/carekeeper_ui.py ~/HealthLink-backup-20260901/
cp ~/HealthLink/carekeeper_style.py ~/HealthLink-backup-20260901/
cp -a ~/HealthLink/style ~/HealthLink-backup-20260901/
```

### 5.3 ส่งเฉพาะไฟล์ Python ของ UI

รันบน Windows PowerShell:

```powershell
scp ".\carekeeper_ui.py" ".\carekeeper_style.py" "raspberry@10.186.252.29:/home/raspberry/HealthLink/"
```

### 5.4 ส่งฟอนต์และรูปในโฟลเดอร์ style

ใช้เมื่อมีการแก้/เพิ่มฟอนต์ ไอคอน หรือ asset:

```powershell
scp -r ".\style" "raspberry@10.186.252.29:/home/raspberry/HealthLink/"
```

### 5.5 ตรวจ syntax บน Pi

```bash
cd ~/HealthLink
source ~/HealthLink/healthlink/bin/activate
python -m py_compile carekeeper_ui.py carekeeper_style.py
```

ถ้าไม่มีข้อความ error ให้เปิดโปรแกรม:

```bash
python main_real.py
```

## 6. กู้ UI เดิมเมื่อไฟล์ใหม่มีปัญหา

ปิดโปรแกรมก่อน แล้วรันบน Pi:

```bash
cp ~/HealthLink-backup-20260901/carekeeper_ui.py ~/HealthLink/
cp ~/HealthLink-backup-20260901/carekeeper_style.py ~/HealthLink/
cp -a ~/HealthLink-backup-20260901/style/. ~/HealthLink/style/
```

จากนั้นตรวจ syntax และเปิดโปรแกรมใหม่:

```bash
cd ~/HealthLink
source ~/HealthLink/healthlink/bin/activate
python -m py_compile carekeeper_ui.py carekeeper_style.py
python main_real.py
```


## 7. ตารางรหัสข้อผิดพลาดของโมดูล (เก็บจากหน้างาน 1 กันยายน 2026)

firmware ส่งรหัสที่โมดูลแจ้ง (`end test,err:<n>`) ต่อมาให้แอปเป็น `BP_ERROR:<n>`
เดิมอ่านรหัสแล้วทิ้ง เหลือแค่ `BP_ERROR` เปล่า ๆ ซึ่งแยกไม่ได้เลยว่าวัดไม่ผ่านเพราะอะไร

เก็บด้วย `sensor_tests/bp_error_codes.py` (รันทีละสถานการณ์ เว้นรอบละ ~2 นาที)
บันทึกดิบอยู่ที่ `sensor_tests/bp-error-codes.log`

| รหัส | สถานการณ์ที่จัดขึ้นจริง | สรุปความหมาย | ข้อความที่ผู้ใช้เห็น |
|---|---|---|---|
| `4` | ไม่ได้พันแขนเลย **และ** ผ้าพันแขนหลวม (ได้รหัสเดียวกันทั้งคู่) | อัดความดันไม่ขึ้น / ความดันรั่ว | ผ้าพันแขนหลวมหรือยังไม่ได้พันแขน — พันให้แน่นพอดีแล้ววัดใหม่ |
| `6` | ขยับแขนระหว่างวัด | อ่านสัญญาณ oscillometric ไม่ได้ | อ่านสัญญาณไม่ได้ (มักเกิดจากขยับแขน/พูดคุย) — นั่งนิ่งแล้ววัดใหม่ |
| `-1` | ไม่ใช่รหัสของโมดูล — firmware สรุปเองเมื่อโมดูลเงียบเกิน 120 วินาที | โมดูลหยุดตอบกลางรอบ | เครื่องวัดหยุดตอบระหว่างวัด — ตรวจสายสัญญาณ |

**ยังไม่ได้เก็บ:** `ถอดสายลม` (รอบที่ลองไปตรงกับตอนโมดูลยังอยู่ใน `WAIT_SHUTDOWN`
จึงยังไม่ได้วัดจริง) เดาไว้ว่าน่าจะได้ `4` เหมือนกันเพราะเป็นอาการอัดความดันไม่ขึ้นเช่นกัน
แต่ยังไม่ยืนยัน

**หลักการของตารางนี้:** รหัสที่ยังไม่เคยเห็นกับตาจะ**ไม่เดาความหมาย** แต่จะแสดงเป็นตัวเลข
ให้ผู้ใช้แจ้งกลับมา (`(รหัสจากเครื่องวัด: N)`) เพราะข้อความที่เดาผิดจะพาเจ้าหน้าที่ไปแก้ผิดจุด
ซึ่งแย่กว่าไม่บอกอะไรเลย ตารางจริงอยู่ที่ `RealCareKeeperProvider._BP_DEVICE_CODE_MESSAGES`
