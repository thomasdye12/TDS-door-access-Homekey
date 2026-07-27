#pragma once

#include <Arduino.h>

// Give each physical bridge a unique name. It appears in DHCP/mDNS.
constexpr char BRIDGE_NAME[] = "homekey-front-door";

// Raw PN532 HSU transport. Keep this LAN-only during the prototype.
constexpr uint16_t BRIDGE_TCP_PORT = 7331;
constexpr uint32_t PN532_BAUD_RATE = 115200;

// Hardware UART pins on the ESP8266:
//   GPIO3 / RX <- PN532 TX (often labelled SDA)
//   GPIO1 / TX -> PN532 RX (often labelled SCL)
//
// The USB serial adapter must be disconnected from GPIO1/GPIO3 after flashing.
constexpr size_t COPY_BUFFER_SIZE = 512;
constexpr uint32_t WIFI_RETRY_INTERVAL_MS = 5000;

// NodeMCU-style ESP8266 boards normally have an active-low LED on LED_BUILTIN.
// Fast blink = joining Wi-Fi, heartbeat = Wi-Fi ready, solid = backend attached.
constexpr uint32_t WIFI_CONNECT_BLINK_MS = 250;
constexpr uint32_t WIFI_READY_HEARTBEAT_MS = 2000;
constexpr uint32_t WIFI_READY_PULSE_MS = 100;
