#include <Arduino.h>
#include <HardwareSerial.h>

#define BP_RX_PIN     16
#define BP_TX_PIN     17
#define BP_SWITCH_PIN 5

HardwareSerial CN3508(2);

enum BPState {
  STATE_IDLE,
  STATE_TRIGGER,
  STATE_MEASURING,
  STATE_WAIT_SHUTDOWN,
  STATE_DONE
};

BPState currentState = STATE_IDLE;

// เวลาที่ยอมให้ค้างอยู่ในแต่ละ state ก่อนจะปลดตัวเองกลับ IDLE
// วัดจากหน้างาน: การวัดจริงใช้ ~50 วิ และโมดูลใช้อีก ~60 วิ กว่าจะส่ง
// MSG_POWER_DOWN จึงตั้งเผื่อไว้เท่าตัว เดิมไม่มี timeout เลย ถ้าโมดูล
// เงียบไปกลางคัน (สายหลุด/ไฟตก) firmware จะติดอยู่ state นั้นตลอดกาล
// และตอบ NOT_READY ทุกครั้งจนกว่าจะถอดสาย USB เสียบใหม่
static const unsigned long MEASURE_TIMEOUT_MS  = 120000;
static const unsigned long SHUTDOWN_TIMEOUT_MS = 120000;

int SYS = 0, DIA = 0, PUL = 0;
bool resultCaptured = false;
bool measureError   = false;

String lineBuffer = "";
unsigned long triggerTime = 0;
bool switchActive = false;
unsigned long stateSince = 0;   // millis() ตอนเข้า state ปัจจุบัน

// ======== State helpers ========
void setState(BPState s) {
  currentState = s;
  stateSince   = millis();
}

const char* stateName(BPState s) {
  switch (s) {
    case STATE_IDLE:          return "IDLE";
    case STATE_TRIGGER:       return "TRIGGER";
    case STATE_MEASURING:     return "MEASURING";
    case STATE_WAIT_SHUTDOWN: return "WAIT_SHUTDOWN";
    case STATE_DONE:          return "DONE";
  }
  return "UNKNOWN";
}

// ======== Parse Hex Result ========
bool parseHexLine(String line) {
  line.trim();
  uint8_t buf[14];
  int count = 0, start = 0;

  for (int i = 0; i <= (int)line.length() && count < 14; i++) {
    if (i == (int)line.length() || line[i] == ' ') {
      String token = line.substring(start, i);
      token.trim();
      if (token.length() == 2) {
        buf[count++] = (uint8_t) strtol(token.c_str(), NULL, 16);
      }
      start = i + 1;
    }
  }

  if (count == 14) {
    SYS = buf[0];
    DIA = buf[1];
    PUL = buf[3];
    return true;
  }
  return false;
}

bool isResultPacket(String line) {
  line.trim();
  int count = 0, start = 0;
  for (int i = 0; i <= (int)line.length(); i++) {
    if (i == (int)line.length() || line[i] == ' ') {
      String token = line.substring(start, i);
      token.trim();
      if (token.length() == 2) {
        bool isHex = true;
        for (int j = 0; j < 2; j++) {
          char c = toupper(token[j]);
          if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F'))) {
            isHex = false; break;
          }
        }
        if (isHex) count++;
        else return false;
      }
      start = i + 1;
    }
  }
  return count == 14;
}

void callback_Result() {
  // Format: "SYS:89,DIA:76,PUL:49"
  Serial.printf("SYS:%d,DIA:%d,PUL:%d\n", SYS, DIA, PUL);
}

// รหัส err ที่โมดูลส่งมาคือคำตอบว่า 'ทำไมวัดไม่ผ่าน' (แขนขยับ / ผ้าพันหลวม /
// ลมรั่ว ฯลฯ) เดิมอ่านออกมาแล้วทิ้ง host จึงบอกผู้ใช้ได้แค่ว่าผิดพลาด
// ค่าติดลบสงวนไว้ให้เหตุที่ firmware สรุปเอง ไม่ใช่รหัสของโมดูล
static const int ERRCODE_MODULE_SILENT = -1;  // เงียบไปกลางคัน watchdog ตัดรอบ

void callback_Error(int errCode) {
  Serial.print("BP_ERROR:");
  Serial.println(errCode);
}

// รอบวัดจบแล้วแต่ไม่เคยได้แพ็กเก็ตผล -- คนละเรื่องกับ BP_ERROR
// (โมดูลไม่ได้แจ้งว่าวัดผิดพลาด) และคนละเรื่องกับเงียบไปเฉย ๆ
void callback_NoResult() {
  Serial.println("NO_RESULT");
}

void callback_Ready() {
  Serial.println("READY");
}

// ปล่อยสวิตช์ทริกเกอร์ให้แน่ใจว่าไม่ค้างกดอยู่ ใช้ตอน RESET และตอน timeout
void releaseTrigger() {
  digitalWrite(BP_SWITCH_PIN, HIGH);
  switchActive = false;
}

// ======== Process Line from BP Module ========
void processBPLine(String line) {
  line.trim();
  if (line.length() == 0) return;

  // filter --> life_cnt non display
  if (line.startsWith("life_cnt")) return;

  switch (currentState) {
    case STATE_MEASURING:
      // Result Packet (14 hex bytes)
      if (isResultPacket(line) && !resultCaptured) {
        if (parseHexLine(line)) {
          resultCaptured = true;
        }
        break;
      }

      // measuring sucess
      if (line.indexOf("measuring process") >= 0) {
        if (resultCaptured) {
          callback_Result();
        } else {
          // จบรอบวัดโดยไม่เคยได้แพ็กเก็ตผล (RX ล้น/บรรทัดขาด) เดิมตรงนี้
          // เงียบสนิท host จึงรอจนหมด timeout แล้วรายงานว่า 'ไม่ตอบสนอง'
          // ทั้งที่ผู้ป่วยโดนรัดแขนครบรอบไปแล้วจริง ๆ
          callback_NoResult();
        }
        setState(STATE_WAIT_SHUTDOWN);
        break;
      }

      // Error
      if (line.indexOf("end test,err:") >= 0) {
        int idx     = line.indexOf("err:");
        int errCode = line.substring(idx + 4).toInt();
        if (errCode != 0) {
          measureError = true;
          callback_Error(errCode);
          setState(STATE_WAIT_SHUTDOWN);
        }
        break;
      }
      break;

    case STATE_WAIT_SHUTDOWN:
      if (line.indexOf("MSG_POWER_DOWN") >= 0) {
        callback_Ready();
        measureError   = false;
        resultCaptured = false;
        setState(STATE_DONE);
      }
      break;

    default:
      break;
  }
}

// ======== Process CMD ========
void processCommand(String cmd) {
  cmd.trim();
  cmd.toUpperCase();

  if (cmd == "START") {
    if (currentState == STATE_IDLE || currentState == STATE_DONE) {
      SYS = DIA = PUL = 0;
      resultCaptured  = false;
      measureError    = false;
      setState(STATE_TRIGGER);
    } else {
      // บอก state ที่ติดอยู่ไปด้วย host จะได้แยกออกว่า "รอบก่อนยังวัดไม่จบ"
      // กับ "รอโมดูลดับเครื่อง" ซึ่งรอไม่เท่ากัน
      Serial.print("NOT_READY:");
      Serial.println(stateName(currentState));
    }
  }
  else if (cmd == "RESET") {
    // ทางเดียวที่ host จะกู้ state ที่ค้างได้ โดยไม่ต้องถอดสาย USB
    // การเปิด serial port ไม่ได้ reset บอร์ดนี้ (พิสูจน์แล้วหน้างาน)
    // state จึงค้างข้ามการปิด-เปิดแอปได้ ซึ่งเป็นที่มาของอาการ
    // "เพิ่งเปิดโปรแกรมแล้ววัดไม่ได้ ต้องรอ 2 นาที"
    releaseTrigger();
    SYS = DIA = PUL = 0;
    resultCaptured = false;
    measureError   = false;
    lineBuffer     = "";
    setState(STATE_IDLE);
    callback_Ready();
  }
  else if (cmd == "STATUS") {
    Serial.print("STATE:");
    Serial.println(stateName(currentState));
  }
}

// ======== Trigger Switch (non-blocking) ========
void doTrigger() {
  if (!switchActive) {
    digitalWrite(BP_SWITCH_PIN, LOW);
    switchActive = true;
    triggerTime  = millis();
  }
  if (switchActive && millis() - triggerTime >= 600) {
    releaseTrigger();
    setState(STATE_MEASURING);
  }
}

// ======== State watchdog ========
// กันไม่ให้ค้างถาวรเมื่อโมดูลเงียบไปกลางคัน
void checkStateTimeout() {
  if (currentState == STATE_MEASURING &&
      millis() - stateSince >= MEASURE_TIMEOUT_MS) {
    releaseTrigger();
    callback_Error(ERRCODE_MODULE_SILENT);  // การวัดรอบนี้ถือว่าล้มเหลว
    setState(STATE_IDLE);
    return;
  }

  if (currentState == STATE_WAIT_SHUTDOWN &&
      millis() - stateSince >= SHUTDOWN_TIMEOUT_MS) {
    // ไม่เคยได้ MSG_POWER_DOWN แต่ปล่อยให้ค้างต่อไม่มีประโยชน์
    callback_Ready();
    measureError   = false;
    resultCaptured = false;
    setState(STATE_IDLE);
  }
}

// ======== Setup ========
void setup() {
  Serial.begin(115200);
  CN3508.begin(115200, SERIAL_8N1, BP_RX_PIN, BP_TX_PIN);

  // readStringUntil() บล็อกจนกว่าจะครบบรรทัดหรือหมด timeout ค่า default คือ
  // 1 วินาที ซึ่งนานพอให้ RX buffer ของ UART2 (256 ไบต์) ล้นจนบรรทัดผลลัพธ์หาย
  Serial.setTimeout(50);

  pinMode(BP_SWITCH_PIN, OUTPUT);
  digitalWrite(BP_SWITCH_PIN, HIGH);

  setState(STATE_IDLE);
  callback_Ready();   // ประกาศตัวตอนบูต host จะได้รู้ว่าบอร์ดเพิ่งเริ่มใหม่
}

// ======== Loop ========
void loop() {

  // Get data BP Module (UART2)
  while (CN3508.available()) {
    char c = CN3508.read();
    if (c == '\n') {
      processBPLine(lineBuffer);
      lineBuffer = "";
    } else if (c != '\r') {
      lineBuffer += c;
      if (lineBuffer.indexOf("measuring process") >= 0) {
        processBPLine(lineBuffer);
        lineBuffer = "";
      }
    }
  }

  // get CMD (UART0)
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    processCommand(cmd);
  }

  if (currentState == STATE_TRIGGER) {
    doTrigger();
  }

  checkStateTimeout();
}
