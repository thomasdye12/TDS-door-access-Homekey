#pragma once

#include <Arduino.h>

constexpr uint16_t BACKEND_PORT = 8765;
constexpr char BACKEND_PATH[] = "/readers";
constexpr uint16_t FIRMWARE_API_PORT = 8766;
constexpr char FIRMWARE_UPDATE_PATH[] = "/firmware/latest";

constexpr uint32_t PN532_BAUD_RATE = 115200;
constexpr uint32_t WIFI_RETRY_INTERVAL_MS = 5000;
constexpr uint32_t WS_RECONNECT_INTERVAL_MS = 2000;
constexpr uint32_t WS_HEARTBEAT_INTERVAL_MS = 10000;
constexpr uint32_t WS_HEARTBEAT_TIMEOUT_MS = 3000;
constexpr uint8_t WS_HEARTBEAT_MISSES = 2;

constexpr size_t MAX_PROTOCOL_PAYLOAD = 1100;
constexpr size_t PN532_RING_SIZE = 2048;
constexpr uint16_t PN532_LOCAL_RESPONSE_TIMEOUT_MS = 1500;
constexpr uint32_t ACCESS_FEEDBACK_DURATION_MS = 1500;
constexpr uint32_t BUTTON_SUCCESS_DURATION_MS = 4000;
constexpr uint32_t BUTTON_FAILURE_DURATION_MS = 1800;
constexpr uint32_t BUTTON_RESULT_TIMEOUT_MS = 3000;
constexpr uint32_t BUTTON_DEBOUNCE_MS = 40;
constexpr uint32_t FIRMWARE_INITIAL_CHECK_DELAY_MS = 60000;
constexpr uint32_t FIRMWARE_CHECK_INTERVAL_MS = 6UL * 60UL * 60UL * 1000UL;

// D1/D2 are safe general-purpose pins now that PN532 uses hardware UART.
// Avoid the old D3/D4 wiring: GPIO0/GPIO2 determine ESP8266 boot mode.
constexpr uint8_t STATUS_LED_PIN = D1;  // GPIO5, external LED, active high.
constexpr uint8_t BUTTON_PIN = D2;      // GPIO4, button to GND, pull-up.

constexpr char FIRMWARE_VERSION[] = "2.4.0";
