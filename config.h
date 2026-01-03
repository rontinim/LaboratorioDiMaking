#pragma once

// Questo file deve contenere SOLO default/valori locali.
// Le stringhe principali (WIFI_*, MQTT_*, ZONE_NAME) preferibilmente arrivano da build_flags per-env.

#ifndef WIFI_SSID
  #define WIFI_SSID "ssid"
#endif

#ifndef WIFI_PASS
  #define WIFI_PASS "pass"
#endif

#ifndef MQTT_HOST
  #define MQTT_HOST "192.168.1.100"
#endif

#ifndef MQTT_PORT
  #define MQTT_PORT 1883
#endif

// NON definire ZONE_NAME qui: lo settiamo in platformio.ini per env:bed1/env:bed2

// Pin relay (se vuoi tenerli qui come default)
#ifndef RELAY_LED_PIN
  #define RELAY_LED_PIN 5
#endif

#ifndef RELAY_PUMP_PIN
  #define RELAY_PUMP_PIN 21
#endif

// Se ti serve ancora questo flag, ok tenerlo
#ifndef RELAY_ACTIVE_LOW
  #define RELAY_ACTIVE_LOW 1
#endif

#ifndef RELAY_LED_ACTIVE_LOW
  #define RELAY_LED_ACTIVE_LOW 1
#endif
