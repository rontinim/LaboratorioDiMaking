#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <HX711.h>
#include <Adafruit_PN532.h>
#include "config.h"


// YL-69
#ifndef SOIL_CISTERN_PIN
  #define SOIL_CISTERN_PIN 33
#endif

#ifndef SOIL_SOIL_PIN
  #define SOIL_SOIL_PIN    35
#endif

// HX711
#ifndef HX_DT
  #define HX_DT  18
#endif

#ifndef HX_SCK
  #define HX_SCK 19
#endif

// PN532 I2C
#ifndef I2C_SDA
  #define I2C_SDA    21
#endif

#ifndef I2C_SCL
  #define I2C_SCL    22
#endif

#ifndef PN532_IRQ
  #define PN532_IRQ   4
#endif

#ifndef PN532_RESET
  #define PN532_RESET 2
#endif

// Rele'
#ifndef RELAY_LED_PIN
  #define RELAY_LED_PIN   5     // attivo LOW (LOW = LED ON)
#endif

#ifndef RELAY_PUMP_PIN
  #define RELAY_PUMP_PIN 23     // attivo HIGH (HIGH = PUMP ON)
#endif

// Motore TB6600
#ifndef PIN_STEP
  #define PIN_STEP 25
#endif

#ifndef PIN_DIR
  #define PIN_DIR  26
#endif

#ifndef PIN_EN
  #define PIN_EN   27
#endif

#ifndef TB_ENABLE_LEVEL
  #define TB_ENABLE_LEVEL   HIGH
#endif

#ifndef TB_DISABLE_LEVEL
  #define TB_DISABLE_LEVEL  LOW
#endif

HX711 scale;
Adafruit_PN532 nfc(PN532_IRQ, PN532_RESET);

WiFiClient net;
PubSubClient mqtt(net);

// YL-69 cisterna
const int CISTERN_DRY_MV = 3200;
const int CISTERN_WET_MV = 2400;

// YL-69 terreno
const int SOIL_DRY_MV = 3130;
const int SOIL_WET_MV = 1700;

// HX711
float LOADCELL_SCALE = -270.76f;

// Motore
long STEPS_PER_REV = 1600;
unsigned int STEP_INTERVAL_US = 800;

bool nfcPresent = false;

bool ledOn  = false;
bool pumpOn = false;

bool motorUp = false;  

uint32_t offAtLed  = 0;
uint32_t offAtPump = 0;

unsigned long lastCycle = 0;
const unsigned long CYCLE_MS = 5000;

float  motorLastRevs = 0.0f;
String motorLastDir  = "none";

char topicTelemetry[80];
char topicCmdRelayLed[80],  topicAckRelayLed[80];
char topicCmdRelayPump[80], topicAckRelayPump[80];
char topicCmdTare[80],      topicAckTare[80];
char topicCmdMotor[80],     topicAckMotor[80];
char topicNfc[80];

static void onMqttMsg(char* topic, byte* payload, unsigned int len);


float mvToPct(int mv, int dry, int wet) {
  if (dry == wet) return 0;
  if (mv > dry) mv = dry;
  if (mv < wet) mv = wet;
  return 100.0f * (float)(dry - mv) / (float)(dry - wet);
}

int cisternThresholdMv() {
  return (CISTERN_DRY_MV + CISTERN_WET_MV) / 2;
}

// Media di pii' letture (filtro semplice)
static int readMvAvg(uint8_t pin, uint8_t samples = 8) {
  uint32_t sum = 0;
  for (uint8_t i = 0; i < samples; i++) {
    sum += analogReadMilliVolts(pin);
    delay(2);
  }
  return (int)(sum / samples);
}

void setRelayLed(bool on, uint32_t secs = 0) {
  ledOn = on;
  pinMode(RELAY_LED_PIN, OUTPUT);
  digitalWrite(RELAY_LED_PIN, on ? LOW : HIGH);   // attivo LOW
  Serial.print("LED   : ");
  Serial.println(on ? "ON" : "OFF");
  offAtLed = secs ? (millis() + secs * 1000UL) : 0;
}

void setRelayPump(bool on, uint32_t ms = 0) {
  pumpOn = on;
  pinMode(RELAY_PUMP_PIN, OUTPUT);
  digitalWrite(RELAY_PUMP_PIN, on ? HIGH : LOW);  // attivo HIGH
  Serial.print("PUMP  : ");
  Serial.println(on ? "ON" : "OFF");
  offAtPump = ms ? (millis() + ms) : 0;
}

static void ackRelay(const char* topic, bool on, bool auto_off=false) {
  StaticJsonDocument<96> ack;
  ack["ok"] = true;
  ack["on"] = on;
  if (auto_off) ack["auto_off"] = true;
  char b[96]; size_t n = serializeJson(ack, b, sizeof(b));
  mqtt.publish(topic, (const uint8_t*)b, n);
}

void stepPulse(unsigned int interval_us) {
  digitalWrite(PIN_STEP, LOW);
  delayMicroseconds(40);
  digitalWrite(PIN_STEP, HIGH);
  delayMicroseconds(interval_us > 40 ? interval_us - 40 : 1);
}

void moveRevs(float revs, int dirLevel) {
  long steps = (long)(revs * STEPS_PER_REV);

  digitalWrite(PIN_DIR, dirLevel);
  delayMicroseconds(150);

  digitalWrite(PIN_EN, TB_ENABLE_LEVEL);
  delay(1);

  for (long i = 0; i < steps; i++) {
    stepPulse(STEP_INTERVAL_US);
  }

  digitalWrite(PIN_EN, TB_DISABLE_LEVEL);
}

void runMotorCommand(float revs, const String& dir) {
  int dirLevel;

  if (dir == "up") {
    dirLevel = LOW;   // LOW = salita
    motorUp  = true;
  } else if (dir == "down") {
    dirLevel = HIGH;
    motorUp  = false;
  } else {  // "toggle" o altro
    dirLevel = motorUp ? HIGH : LOW;
    motorUp  = !motorUp;
  }

  moveRevs(revs, dirLevel);

  motorLastRevs = revs;
  motorLastDir  = (dirLevel == HIGH) ? "down" : "up";

  Serial.print("MOTOR : ");
  Serial.print(revs);
  Serial.print(" revs DIR=");
  Serial.println(motorLastDir);
}


void readCistern(int &mvOut, bool &acquaOut, float &pctOut) {
  const int     HYST_MV = 160;       
  const uint8_t CONFIRM_READS = 3;   
  const uint8_t AVG_SAMPLES   = 8;   

  static bool waterState = false;    
  static uint8_t pending = 0;
  static bool initialized = false;

  int mv = readMvAvg(SOIL_CISTERN_PIN, AVG_SAMPLES);

  int th     = cisternThresholdMv();
  int onTh   = th - (HYST_MV / 2);   // sotto questa acqua
  int offTh  = th + (HYST_MV / 2);   // sopra questa manca acqua

  if (!initialized) {
    waterState = (mv <= th);
    pending = 0;
    initialized = true;
  }

  // Percentuale come prima
  float pct = mvToPct(mv, CISTERN_DRY_MV, CISTERN_WET_MV);

  bool desired = waterState;
  if (!waterState && mv <= onTh) desired = true;      // rientra in acqua
  if ( waterState && mv >= offTh) desired = false;    // passa a no acqua

  // Debounce su N cicli
  if (desired != waterState) {
    if (++pending >= CONFIRM_READS) {
      waterState = desired;
      pending = 0;
    }
  } else {
    pending = 0;
  }

  mvOut    = mv;
  acquaOut = waterState;
  pctOut   = pct;
}

void readSoil(int &mvOut, float &pctOut) {
  int mv = analogReadMilliVolts(SOIL_SOIL_PIN);
  float pct = mvToPct(mv, SOIL_DRY_MV, SOIL_WET_MV);

  mvOut  = mv;
  pctOut = pct;
}

bool readScale(float &gOut) {
  if (scale.is_ready()) {
    gOut = scale.get_units(10);
    return true;
  }
  return false;
}

void readNFC(String &uidOut, bool &hasTagOut) {
  hasTagOut = false;
  uidOut = "";

  if (!nfcPresent) {
    uidOut = "NFC_OFF";
    return;
  }

  uint8_t uid[7];
  uint8_t uidLength;

  if (nfc.readPassiveTargetID(
        PN532_MIFARE_ISO14443A,
        uid,
        &uidLength,
        400)) {

    uidOut = "";
    for (int i = 0; i < uidLength; i++) {
      if (i > 0) uidOut += ":";
      if (uid[i] < 0x10) uidOut += "0";
      uidOut += String(uid[i], HEX);
    }
    uidOut.toUpperCase();
    hasTagOut = true;
  } else {
    uidOut = "NO_TAG";
  }
}

static void wifiConnect() {
  if (WiFi.status() == WL_CONNECTED) return;

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi connecting");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println(" OK");
}

static void mqttConnect() {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);

  char cid[64];
  snprintf(cid, sizeof(cid), "esp32-%s-%08lx", ZONE_NAME, (unsigned long)ESP.getEfuseMac());

  while (!mqtt.connected()) {
    Serial.print("[MQTT] connecting...");
    if (mqtt.connect(cid)) {
      Serial.println(" OK");

      if (topicCmdRelayLed[0])  mqtt.subscribe(topicCmdRelayLed);
      if (topicCmdRelayPump[0]) mqtt.subscribe(topicCmdRelayPump);
      if (topicCmdTare[0])      mqtt.subscribe(topicCmdTare);
      if (topicCmdMotor[0])     mqtt.subscribe(topicCmdMotor);

    } else {
      Serial.print(" FAIL rc=");
      Serial.println(mqtt.state());
      delay(800);
    }
  }
}

// Callback MQTT
static void onMqttMsg(char* topic, byte* payload, unsigned int len) {
  StaticJsonDocument<256> doc;
  DeserializationError e = deserializeJson(doc, payload, len);
  if (e) {
    Serial.print("JSON parse error: ");
    Serial.println(e.c_str());
    return;
  }

  // RELAY LED
  if (strcmp(topic, topicCmdRelayLed) == 0) {
    bool on  = doc["on"]   | false;
    uint32_t secs = doc["secs"] | 0;
    setRelayLed(on, secs);
    ackRelay(topicAckRelayLed, ledOn, false);
    return;
  }

  // RELAY PUMP con supporta decimali
  if (strcmp(topic, topicCmdRelayPump) == 0) {
    bool on  = doc["on"] | false;

    // Legge secs come numero reale (JSON con punto, es: 0.6)
    double secs = doc["secs"] | 0.0;

    // Converte in millisecondi (arrotonda)
    uint32_t ms = (secs > 0.0) ? (uint32_t)(secs * 1000.0 + 0.5) : 0;

    setRelayPump(on, ms);
    ackRelay(topicAckRelayPump, pumpOn, false);
    return;
  }

  // TARE bilancia
  if (strcmp(topic, topicCmdTare) == 0) {
    bool doIt = doc["tare"] | false;
    StaticJsonDocument<64> ack; ack["ok"] = false;
    if (doIt) {
      scale.tare(20);
      ack["ok"] = true;
    }
    char b[64]; size_t n = serializeJson(ack,b,sizeof(b));
    mqtt.publish(topicAckTare,(const uint8_t*)b,n);
    return;
  }

  // MOTOR command
  if (strcmp(topic, topicCmdMotor) == 0) {
    float revs = doc["revs"] | 0.0f;
    const char* dir = doc["dir"] | "toggle";

    StaticJsonDocument<128> ack; ack["ok"] = false;

    if (revs > 0.0f) {
      String dirStr(dir);
      runMotorCommand(revs, dirStr);
      ack["ok"]        = true;
      ack["revs"]      = revs;
      ack["dir"]       = motorLastDir;
    }

    char b[128]; size_t n = serializeJson(ack,b,sizeof(b));
    mqtt.publish(topicAckMotor,(const uint8_t*)b,n);
    return;
  }
}

static void publishTelemetry() {
  StaticJsonDocument<512> doc;

  // CISTERNA
  int cis_mv; bool cis_acqua; float cis_pct;
  readCistern(cis_mv, cis_acqua, cis_pct);
  doc["soil_cistern_mv"]  = cis_mv;
  doc["soil_cistern_pct"] = cis_pct;
  doc["soil_cistern_ok"]  = cis_acqua;

  // TERRENO
  int soil_mv; float soil_pct;
  readSoil(soil_mv, soil_pct);
  doc["soil_mv"]   = soil_mv;
  doc["soil_pct"]  = soil_pct;

  // BILANCIA
  float g;
  if (readScale(g)) doc["weight_g"] = g;
  else              doc["weight_g"] = nullptr;

  // REL�
  doc["relay_led"]  = ledOn ? 1 : 0;
  doc["relay_pump"] = pumpOn ? 1 : 0;

  // MOTORE
  doc["motor_last_dir"]  = motorLastDir;
  doc["motor_last_revs"] = motorLastRevs;

  // NFC
  String uid; bool hasTag;
  readNFC(uid, hasTag);
  doc["nfc_uid"]     = uid;
  doc["nfc_present"] = nfcPresent;
  doc["nfc_has_tag"] = hasTag;

  // stato rete PRIMA
  Serial.print("WiFi status="); Serial.println((int)WiFi.status());
  Serial.print("MQTT connected="); Serial.print(mqtt.connected() ? "true" : "false");
  Serial.print(" state="); Serial.println(mqtt.state());

  // publish
  char buf[512];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  bool ok = mqtt.publish(topicTelemetry, (const uint8_t*)buf, n);

  Serial.print(ok ? "PUB " : "PUB(FAIL) ");
  Serial.print(topicTelemetry); Serial.print(" ");
  serializeJson(doc, Serial); Serial.println();

  if (!ok) {
    Serial.print("publish failed -> mqtt.connected=");
    Serial.print(mqtt.connected() ? "true" : "false");
    Serial.print(" state=");
    Serial.println(mqtt.state());
  }

  // NFC dedicato
  if (hasTag && uid != "NO_TAG" && uid != "NFC_OFF") {
    StaticJsonDocument<96> nfcDoc;
    nfcDoc["uid"] = uid;
    char nb[128]; size_t nn = serializeJson(nfcDoc, nb, sizeof(nb));
    bool nfcOk = mqtt.publish(topicNfc, (const uint8_t*)nb, nn);
    Serial.print(nfcOk ? "PUB NFC " : "PUB NFC(FAIL) ");
    Serial.println(uid);
    if (!nfcOk) {
      Serial.print("nfc publish failed -> state=");
      Serial.println(mqtt.state());
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println("\n=== SERRA COMPLETA MQTT - SETUP ===");

  // ADC
  analogReadResolution(12);
  analogSetPinAttenuation(SOIL_CISTERN_PIN, ADC_11db);
  analogSetPinAttenuation(SOIL_SOIL_PIN,    ADC_11db);

  // RELE'
  pinMode(RELAY_LED_PIN, OUTPUT);
  pinMode(RELAY_PUMP_PIN, OUTPUT);
  setRelayLed(false, 0);
  setRelayPump(false, 0);

  // MOTORE
  pinMode(PIN_STEP, OUTPUT);
  pinMode(PIN_DIR,  OUTPUT);
  pinMode(PIN_EN,   OUTPUT);
  digitalWrite(PIN_STEP, HIGH);
  digitalWrite(PIN_DIR,  LOW);
  digitalWrite(PIN_EN, TB_DISABLE_LEVEL);

  // HX711
  scale.begin(HX_DT, HX_SCK);
  scale.wait_ready_timeout(2000);
  scale.set_scale(1.0f);
  scale.tare(20);
  scale.set_scale(LOADCELL_SCALE);

  // NFC
  Wire.begin(I2C_SDA, I2C_SCL);
  nfc.begin();
  uint32_t ver = nfc.getFirmwareVersion();
  if (!ver) {
    nfcPresent = false;
    Serial.println("NFC: NON PRESENTE");
  } else {
    nfcPresent = true;
    nfc.SAMConfig();
    Serial.println("NFC: OK");
  }

  // MQTT topics (stile vecchio codice)
  snprintf(topicTelemetry,   sizeof(topicTelemetry),   "serra/telemetry/%s",     ZONE_NAME);
  snprintf(topicCmdRelayLed, sizeof(topicCmdRelayLed), "serra/cmd/%s/relay/led", ZONE_NAME);
  snprintf(topicAckRelayLed, sizeof(topicAckRelayLed), "serra/ack/%s/relay/led", ZONE_NAME);
  snprintf(topicCmdRelayPump,sizeof(topicCmdRelayPump),"serra/cmd/%s/relay/pump",ZONE_NAME);
  snprintf(topicAckRelayPump,sizeof(topicAckRelayPump),"serra/ack/%s/relay/pump",ZONE_NAME);
  snprintf(topicCmdTare,     sizeof(topicCmdTare),     "serra/cmd/%s/tare",      ZONE_NAME);
  snprintf(topicAckTare,     sizeof(topicAckTare),     "serra/ack/%s/tare",      ZONE_NAME);
  snprintf(topicCmdMotor,    sizeof(topicCmdMotor),    "serra/cmd/%s/motor",     ZONE_NAME);
  snprintf(topicAckMotor,    sizeof(topicAckMotor),    "serra/ack/%s/motor",     ZONE_NAME);
  snprintf(topicNfc,         sizeof(topicNfc),         "serra/nfc/%s",           ZONE_NAME);

  mqtt.setCallback(onMqttMsg);

  // NET
  wifiConnect();
  Serial.printf("TARGET MQTT %s:%d zone=%s\n", MQTT_HOST, MQTT_PORT, ZONE_NAME);
  mqttConnect();

  Serial.println("=== SETUP COMPLETATO ===\n");
}

void loop() {
  // rete
  if (WiFi.status() != WL_CONNECTED) wifiConnect();
  if (!mqtt.connected())             mqttConnect();
  mqtt.loop();

  // auto-off rele'
  uint32_t now = millis();
  if (offAtLed && (int32_t)(now - offAtLed) >= 0) {
    setRelayLed(false, 0);
    offAtLed = 0;
    ackRelay(topicAckRelayLed, ledOn, true);
  }
  if (offAtPump && (int32_t)(now - offAtPump) >= 0) {
    setRelayPump(false, 0);
    offAtPump = 0;
    ackRelay(topicAckRelayPump, pumpOn, true);
  }

  // ciclo telemetria
  if (now - lastCycle >= CYCLE_MS) {
    lastCycle = now;

    Serial.println("================================");
    publishTelemetry();
    Serial.println("================================\n");
  }
}
