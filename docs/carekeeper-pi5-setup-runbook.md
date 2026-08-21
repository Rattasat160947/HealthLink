# CareKeeper — คู่มือตั้งค่าเครื่อง Pi 5 ใหม่ (Setup Runbook)

รวมขั้นตอนที่จำเป็นสำหรับตั้งเครื่อง Raspberry Pi 5 ใหม่ทั้งใบ ทำตามลำดับ Phase 0 – 12 ได้เลย
**เครื่องหมาย `<USER>` ทุกจุดในเอกสารนี้ให้แทนด้วยชื่อ user จริงที่ใช้ (เช่น `cpe05`)**

> อัปเดตล่าสุด: 2026-08-21 — ปรับให้ตรงกับโค้ดปัจจุบัน (venv ต้องเห็น `smbus` ของระบบ, ติดตั้งจาก `requirement.txt`, ต้องมีไฟล์ `.env`, BP port auto-detect)

---

## Phase 0 — ก่อนเริ่ม (Raspberry Pi Imager)

แฟลช SD/SSD ด้วย **Raspberry Pi Imager** เลือก **Raspberry Pi OS (64-bit) แบบมี Desktop** แล้วกดปุ่มเฟือง (⚙️) ตั้งค่าให้ครบตั้งแต่ก่อนแฟลช:

- hostname, username / password (ใช้ชื่อนี้แทน `<USER>` ตลอดเอกสาร)
- Wi-Fi SSID / password + country, locale / timezone
- เปิด **Enable SSH**

ตั้งครบตั้งแต่ตรงนี้ = first-run wizard (`piwiz`) จะไม่เด้งขึ้นมาตอนบูตครั้งแรก ไม่ต้องมานั่งกดทีหลัง

---

## Phase 1 — ตั้งค่าระบบพื้นฐานผ่าน raspi-config

```bash
sudo raspi-config
```

- **System Options → Boot / Auto Login → Desktop Autologin** (ให้ boot เข้า desktop อัตโนมัติ)
- **Interface Options → VNC → Enable** (ไว้รีโมตเข้ามาดูหน้าจอ kiosk)
- **Interface Options → I2C → Enable** (จำเป็นสำหรับ UPS HAT ที่ใช้อ่านเปอร์เซ็นต์แบตเตอรี่)
- **Interface Options → SSH → Enable** (ถ้ายังไม่ได้เปิดจาก Imager)

reboot แล้วตรวจว่า login เข้า desktop เองได้ และต่อ VNC/SSH จากเครื่องอื่นได้ปกติ (ให้ผ่าน baseline ตรงนี้ก่อนค่อยไป phase ถัดไป)

```bash
sudo reboot
```

> ⚠️ ถ้ารีโมตเข้าเครื่องผ่าน Wi-Fi (VNC/SSH) **ห้ามกดปิด Wi-Fi หรือสลับ SSID จากในแอป** ระหว่าง setup เพราะจะหลุดการเชื่อมต่อและเข้าเครื่องไม่ได้อีก ต้องไปต่อจอ+คีย์บอร์ดที่ตัวเครื่องอย่างเดียว

---

## Phase 2 — ติดตั้ง package ระดับ OS

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git python3 python3-venv python3-pip python3-dev pcscd pcsc-tools libccid libpcsclite-dev swig python3-smbus i2c-tools bluetooth bluez python3-pil
```

| กลุ่ม | ใช้ทำอะไร |
| --- | --- |
| `pcscd`, `pcsc-tools`, `libccid` | daemon + เครื่องมือทดสอบเครื่องอ่านบัตรประชาชน |
| `libpcsclite-dev`, `swig`, `python3-dev` | ต้องมีตอน `pip install pyscard` (คอมไพล์จาก source) |
| `python3-smbus`, `i2c-tools` | อ่านค่า UPS HAT ผ่าน I2C (`lib/ups.py` import `smbus`) |
| `bluetooth`, `bluez` | BLE สำหรับ SpO2 (H59) + คำสั่ง `bluetoothctl` ที่แอปเรียกใช้ |
| `python3-pil` | ใช้ตอนทำ splash เป็นสีเรียบใน Phase 11.3 |

---

## Phase 3 — ดึงโค้ดโปรเจกต์ลงเครื่อง

```bash
git clone https://github.com/Rattasat160947/Care_Keeper.git ~/Care_Keeper
```

(ถ้าไม่ได้ต่อเน็ต ให้ก็อปทั้งโฟลเดอร์ผ่าน USB / `scp` มาวางที่ `/home/<USER>/Care_Keeper/` แทน — **ยกเว้น `.venv/` ของเครื่อง Windows ห้ามก็อปมา** ต้องสร้าง venv ใหม่บน Pi เสมอ)

ไฟล์ที่ต้องมีครบ:

```
Care_Keeper/
├── main_real.py, main_demo.py
├── carekeeper_ui.py, carekeeper_providers.py, carekeeper_style.py
├── carekeeper_queue.py, carekeeper_retry.py, carekeeper_logging.py
├── requirement.txt, .env.example
├── style/          <-- ฟอนต์ไทย/อังกฤษ + ไอคอน (ขาดแล้ว UI เพี้ยน)
├── conf/           <-- ตัวอย่างไฟล์ systemd
└── lib/
    ├── ups.py, bp_monitor.py
    ├── h59_ble/    (__init__.py, device.py, spo2.py, heart_rate.py)
    └── thaiidcard/ (card.py, apdu.py, init.py)
```

> โฟลเดอร์ `data/` ไม่ต้องสร้างเอง โปรแกรมสร้างให้ตอนรันครั้งแรก (เก็บคิวส่งข้อมูลออฟไลน์ `carekeeper_queue.db`)

---

## Phase 4 — สร้าง Python venv + ลง package

**ต้องใช้ `--system-site-packages`** เพราะ `lib/ups.py` เรียก `smbus` ที่มาจาก apt (`python3-smbus`) ซึ่งไม่มีบน PyPI ถ้าสร้าง venv แบบปิดจะอ่านเปอร์เซ็นต์แบตไม่ได้

```bash
cd ~/Care_Keeper && python3 -m venv --system-site-packages carekeeper
```

```bash
cd ~/Care_Keeper && source carekeeper/bin/activate && pip install --upgrade pip && pip install -r requirement.txt
```

`requirement.txt` = `PySide6`, `pyqtgraph`, `bleak`, `pyserial`, `pyscard`, `requests`, `python-dotenv`

ตรวจว่าลงครบจริง:

```bash
~/Care_Keeper/carekeeper/bin/python3 -c "import PySide6, pyqtgraph, bleak, serial, smartcard, requests, dotenv, smbus; print('OK')"
```

> ถ้า `pip install PySide6` ล้มเหลว (ไม่มี wheel สำหรับ OS รุ่นนั้น) ให้ใช้ของระบบแทน — `sudo apt install -y python3-pyside6.qtwidgets python3-pyside6.qtcharts` แล้วลง pip เฉพาะตัวที่เหลือ เพราะ venv ที่สร้างแบบ `--system-site-packages` จะมองเห็น PySide6 ของระบบได้เอง
> ถ้า `pip install pyscard` ล้มเหลว ให้ย้อนไปเช็คว่า Phase 2 ลง `libpcsclite-dev` + `swig` + `python3-dev` ครบหรือยัง

---

## Phase 5 — ตั้งค่าไฟล์ `.env`

```bash
cd ~/Care_Keeper && cp .env.example .env && nano .env
```

```env
CAREKEEPER_API_URL=https://telemed-be-maua72ti2a-as.a.run.app/api/v2/device/add_health
CAREKEEPER_API_KEY_HEADER=api-key
CAREKEEPER_API_KEY=<ใส่ key จริงของเครื่องนี้>

CAREKEEPER_HISTORY_API_URL=https://telemed-be-maua72ti2a-as.a.run.app/api/v2/device/health_history
CAREKEEPER_HISTORY_PATIENT_ID_PARAM=patient_id
CAREKEEPER_HISTORY_MAC_PARAM=mac

CAREKEEPER_BP_PORT=auto
CAREKEEPER_H59_DEVICE_NAME=H59_D105
CAREKEEPER_H59_DEVICE_ADDRESS=
```

- **`CAREKEEPER_BP_PORT`** — ใส่ `auto` ได้เลย โค้ดจะสแกนหา USB-serial ของเครื่องวัดความดันเอง (เลข `/dev/ttyUSB0` / `ttyUSB1` สลับได้โดยไม่ต้องแก้ไฟล์) จะระบุพอร์ตตรง ๆ ก็ได้ถ้ามี USB-serial หลายตัวเสียบพร้อมกัน
- **`CAREKEEPER_H59_DEVICE_ADDRESS`** — ค่าตัวอย่างในไฟล์เป็น UUID ของ macOS **ใช้บน Pi ไม่ได้** บน Linux ต้องเป็น BLE MAC (`XX:XX:XX:XX:XX:XX`) ปล่อยว่างไว้ก็ได้ เพราะปกติโปรแกรมสแกนหาจากชื่อใน `CAREKEEPER_H59_DEVICE_NAME` ก่อนอยู่แล้ว หา MAC ได้ด้วย `bluetoothctl scan on` แล้วรอจนเห็นชื่อ `H59_D105`
- ไฟล์ `.env` อยู่ใน `.gitignore` → ต้องตั้งใหม่ทุกเครื่อง ไม่ติดมากับ `git clone`

---

## Phase 6 — สิทธิ์เข้าถึงอุปกรณ์ (USB / I2C / Bluetooth)

```bash
sudo usermod -aG dialout,bluetooth,i2c,plugdev $USER
```

```bash
sudo tee /etc/udev/rules.d/99-thaiidcard.rules > /dev/null << 'RULES'
SUBSYSTEM=="usb", ENV{ID_USB_INTERFACES}=="*:0b0000:*", MODE="0666"
SUBSYSTEM=="usb", MODE="0666"
RULES
```

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

สิทธิ์กลุ่มจะมีผลจริงหลัง **logout/reboot** เท่านั้น

---

## Phase 7 — polkit rule ให้ pcscd อนุญาต client ที่ไม่มี active session

(จุดนี้สำคัญมาก ถ้าข้ามจะเจอ "access denied" ตอนอ่านบัตร เพราะ pcscd default จะอนุญาตเฉพาะ session ที่ login มีหน้าจอจริงเท่านั้น)

```bash
sudo tee /etc/polkit-1/rules.d/50-pcscd.rules > /dev/null << 'RULES'
polkit.addRule(function(action, subject) {
    if (action.id == "org.debian.pcsc-lite.access_pcsc" ||
        action.id == "org.debian.pcsc-lite.access_card") {
        return polkit.Result.YES;
    }
});
RULES
```

```bash
sudo systemctl enable --now pcscd && sudo systemctl restart polkit && sudo systemctl restart pcscd
```

---

## Phase 8 — ทดสอบฮาร์ดแวร์ทีละตัว (ก่อนตั้ง kiosk)

เสียบอุปกรณ์ให้ครบ (เครื่องอ่านบัตร, สาย USB ของเครื่องวัดความดัน, UPS HAT) แล้วเช็ค:

```bash
pcsc_scan
```

```bash
ls -l /dev/ttyUSB*
```

```bash
i2cdetect -y 1
```

- `pcsc_scan` → ต้องเห็นชื่อ reader และตอบสนองตอนเสียบบัตร (Ctrl+C ออก)
- `ls /dev/ttyUSB*` → ต้องเห็นพอร์ตของเครื่องวัดความดัน
- `i2cdetect -y 1` → ต้องเห็นแอดเดรส `2d` = UPS HAT

แล้วลองรันจริงจากหน้าเดสก์ท็อป (หรือผ่าน VNC) ก่อนไปตั้ง autostart:

```bash
cd ~/Care_Keeper && ./carekeeper/bin/python3 main_real.py
```

อยากลองเฉพาะ UI โดยไม่ต่ออุปกรณ์ ใช้ `main_demo.py` แทน ส่วนสคริปต์ทดสอบอุปกรณ์แยกตัวอยู่ใน `sensor_tests/` (`idcard.py`, `BP.py`, `H59_BLE.py`, `battery.py`, `ble_scaner.py`)

---

## Phase 9 — จำกัด log ไม่ให้กิน disk

```bash
sudo mkdir -p /etc/systemd/journald.conf.d && sudo tee /etc/systemd/journald.conf.d/carekeeper-journal.conf > /dev/null << 'CONF'
[Journal]
SystemMaxUse=200M
MaxRetentionSec=14day
CONF
```

```bash
sudo systemctl restart systemd-journald
```

---

## Phase 10 — Reboot อัตโนมัติตอนตี 3 (กัน memory/cache คั่งค้างจากรันยาว)

```bash
sudo tee /etc/systemd/system/carekeeper-restart.service > /dev/null << 'UNIT'
[Unit]
Description=Nightly reboot to keep memory/cache fresh on Pi5

[Service]
Type=oneshot
ExecStart=/bin/systemctl reboot
UNIT
```

```bash
sudo tee /etc/systemd/system/carekeeper-restart.timer > /dev/null << 'UNIT'
[Unit]
Description=Run CareKeeper nightly restart at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now carekeeper-restart.timer
```

> หมายเหตุ: ไฟล์ตัวอย่าง `conf/carekeeper-restart.service` ใน repo เขียนไว้เป็น `systemctl restart carekeeper.service` ซึ่งใช้กับแนวทาง systemd service แบบเดิม **แนวทางที่ใช้จริงตอนนี้คือ autostart ของ labwc (Phase 11)** จึงต้องใช้เวอร์ชัน `systemctl reboot` ด้านบนนี้ ไม่ใช่ก็อปไฟล์จาก `conf/` มาตรง ๆ

---

## Phase 11 — Kiosk mode: boot ตรงเข้าโปรแกรม ไม่เห็นเดสก์ท็อป

**ห้ามสร้าง systemd service ใหม่สำหรับหน้าจอ (`carekeeper.service`)** — เคยลองแล้วไม่เสถียรกับ Wayland/labwc ให้แก้ที่ autostart ของ labwc แทน

**11.1 แก้ system autostart ของ labwc (ไฟล์ที่สำคัญที่สุด ห้ามลืม):**

```bash
sudo nano /etc/xdg/labwc/autostart
```

เดิมจะมี:

```
/usr/bin/lwrespawn /usr/bin/pcmanfm-pi &
/usr/bin/lwrespawn /usr/bin/wf-panel-pi &
/usr/bin/kanshi &
/usr/bin/lxsession-xdg-autostart
```

แก้เป็น (comment 2 บรรทัดแรกออก = ไม่มี desktop icon และ taskbar):

```
# /usr/bin/lwrespawn /usr/bin/pcmanfm-pi &
# /usr/bin/lwrespawn /usr/bin/wf-panel-pi &
/usr/bin/kanshi &
/usr/bin/lxsession-xdg-autostart
```

> labwc อ่านทั้งไฟล์ system (`/etc/xdg/labwc/autostart`) และไฟล์ user (`~/.config/labwc/autostart`) **พร้อมกันทั้ง 2 ไฟล์** ไม่ได้เลือกอันใดอันหนึ่ง จึงต้องแก้ไฟล์ system ตรงนี้ด้วยเสมอ ไม่ใช่แก้แต่ไฟล์ user

**11.2 สร้างไฟล์ autostart ของ user สำหรับเปิดโปรแกรมเรา:**

```bash
mkdir -p ~/.config/labwc && cat > ~/.config/labwc/autostart << 'AUTOSTART'
(
  while true; do
    /home/<USER>/Care_Keeper/carekeeper/bin/python3 /home/<USER>/Care_Keeper/main_real.py
    sleep 2
  done
) &
AUTOSTART
```

```bash
chmod +x ~/.config/labwc/autostart
```

> **ห้ามลืม `chmod +x`** — ถ้าไฟล์นี้ไม่มีสิทธิ์ executable labwc จะไม่รัน script เลย (เคยติดปัญหานี้มาแล้ว)
> และอย่าลืมแทน `<USER>` ด้วยชื่อ user จริงทั้ง 2 จุดในไฟล์

**11.3 เอาข้อความ "Welcome to the Raspberry Pi Desktop" ตอนบูตออก**

ข้อความนี้เป็นภาพฝังอยู่ในไฟล์ `splash.png` ของ Plymouth ไม่ใช่ text จึงต้องทับด้วยภาพสีเรียบ

```bash
sudo cp /usr/share/plymouth/themes/pix/splash.png /usr/share/plymouth/themes/pix/splash.png.bak
```

```bash
sudo python3 -c "from PIL import Image; img = Image.open('/usr/share/plymouth/themes/pix/splash.png.bak'); bg = img.convert('RGB').getpixel((2, 2)); Image.new('RGB', img.size, bg).save('/usr/share/plymouth/themes/pix/splash.png'); print('Done', img.size, bg)"
```

คืนค่าเดิมได้ตลอดด้วย:

```bash
sudo cp /usr/share/plymouth/themes/pix/splash.png.bak /usr/share/plymouth/themes/pix/splash.png
```

> จอ **"No signal"** ที่อาจเห็นก่อนหน้านั้น เป็นข้อความของตัวจอเอง ไม่เกี่ยวกับ Pi/Plymouth

---

## Phase 12 — Reboot แล้วเช็คให้ครบ

```bash
sudo reboot
```

เช็คหลัง reboot:

- [ ] บูตแล้วเข้าโปรแกรม CareKeeper เอง ไม่เห็น wallpaper / taskbar
- [ ] ตอนบูตไม่มีโลโก้/ข้อความ "Welcome to the Raspberry Pi Desktop" (11.3)
- [ ] กด "เริ่มอ่านค่าบัตรประชาชน" แล้วขึ้นชื่อ-สกุล และวันเกิดเป็น "1 มกราคม 2530" (ไม่ใช่ `25300101`)
- [ ] วัดความดันได้ (ต่อ BP ผ่าน USB โดยไม่ต้องแก้ `.env`)
- [ ] วัด SpO2 / ชีพจรผ่าน BLE (H59) ได้
- [ ] แถบสถานะแสดงเปอร์เซ็นต์แบตจาก UPS HAT (ถ้าไม่ขึ้น = I2C ยังไม่เปิด หรือ venv ไม่เห็น `smbus`)
- [ ] กดบันทึกแล้วข้อมูลขึ้น backend (ถ้าเน็ตหลุด ต้องเข้าคิวใน `data/carekeeper_queue.db` แล้วส่งเองเมื่อเน็ตกลับมา)
- [ ] ต่อ VNC จากเครื่องอื่นได้ปกติ
- [ ] `systemctl status carekeeper-restart.timer` → active
- [ ] `ps aux | grep -E "pcmanfm-pi|wf-panel-pi"` → ไม่เจอ

---

## ภาคผนวก — ปัญหาที่เจอบ่อย

| อาการ | สาเหตุ / วิธีแก้ |
| --- | --- |
| อ่านบัตรแล้ว "access denied" | ข้าม Phase 7 (polkit) หรือ `pcscd` ยังไม่ enable → `sudo systemctl enable --now pcscd` |
| หาเครื่องอ่านบัตรไม่เจอ | udev rule (Phase 6) ยังไม่ถูกโหลด หรือยังไม่ได้ reboot หลัง `usermod` |
| แบตเตอรี่ไม่ขึ้น | I2C ยังไม่เปิดใน raspi-config, `i2cdetect -y 1` ไม่เห็น `2d`, หรือ venv สร้างโดยไม่มี `--system-site-packages` |
| วัดความดันไม่ได้ | ไม่มี `/dev/ttyUSB*` หรือ user ยังไม่อยู่ในกลุ่ม `dialout` |
| SpO2 ต่อไม่ติด | ชื่อใน `CAREKEEPER_H59_DEVICE_NAME` ไม่ตรงกับที่นาฬิกา advertise / นาฬิกาแบตหมด / ค้างที่การจับคู่เดิม → `bluetoothctl remove <MAC>` แล้วลองใหม่ |
| จอค้างที่เดสก์ท็อป ไม่เปิดโปรแกรม | `~/.config/labwc/autostart` ไม่ได้ `chmod +x` หรือยังไม่ได้แทน `<USER>` เป็นชื่อจริง |
| ส่งข้อมูลไม่ขึ้น backend | ยังไม่ได้สร้าง `.env` หรือ `CAREKEEPER_API_KEY` ผิด |

ดู log ได้จาก `journalctl -b` หรือรันมือด้วย `./carekeeper/bin/python3 main_real.py` แล้วดูข้อความใน terminal
