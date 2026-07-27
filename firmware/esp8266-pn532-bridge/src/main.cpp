#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266mDNS.h>

#include "bridge_config.h"
#include "secrets.h"

#ifndef SERIAL_DIAGNOSTICS
#define SERIAL_DIAGNOSTICS 0
#endif

WiFiServer bridgeServer(BRIDGE_TCP_PORT);
WiFiClient backendClient;

uint8_t copyBuffer[COPY_BUFFER_SIZE];
uint32_t lastWifiAttemptAt = 0;
bool mdnsStarted = false;
uint32_t lastDiagnosticAt = 0;

static void updateStatusLed() {
  if (WiFi.status() != WL_CONNECTED) {
    digitalWrite(
        LED_BUILTIN,
        ((millis() / WIFI_CONNECT_BLINK_MS) % 2) ? LOW : HIGH
    );
    return;
  }

  if (backendClient && backendClient.connected()) {
    digitalWrite(LED_BUILTIN, LOW);
    return;
  }

  digitalWrite(
      LED_BUILTIN,
      (millis() % WIFI_READY_HEARTBEAT_MS) < WIFI_READY_PULSE_MS ? LOW : HIGH
  );
}

static void connectWifiIfNeeded() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  const uint32_t now = millis();
  if (lastWifiAttemptAt != 0 &&
      now - lastWifiAttemptAt < WIFI_RETRY_INTERVAL_MS) {
    return;
  }

  lastWifiAttemptAt = now;
  WiFi.disconnect();
  WiFi.hostname(BRIDGE_NAME);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

static void startNetworkServicesIfNeeded() {
  if (WiFi.status() != WL_CONNECTED || mdnsStarted) {
    return;
  }

  bridgeServer.begin();
  bridgeServer.setNoDelay(true);

  mdnsStarted = MDNS.begin(BRIDGE_NAME);
  if (mdnsStarted) {
    MDNS.addService("homekey-pn532", "tcp", BRIDGE_TCP_PORT);
  }
}

static void acceptBackend() {
  if (!bridgeServer.hasClient()) {
    return;
  }

  WiFiClient candidate = bridgeServer.accept();
  if (backendClient && backendClient.connected()) {
    // Exactly one backend may own the PN532 UART at a time.
    candidate.stop();
    return;
  }

  backendClient = candidate;
  backendClient.setNoDelay(true);
  backendClient.keepAlive(5, 3, 3);

  // Do not deliver bytes left over from a previous, interrupted NFC session.
  while (Serial.available()) {
    Serial.read();
  }
}

static void copySocketToPn532() {
  if (!backendClient || !backendClient.connected()) {
    return;
  }

  int available = backendClient.available();
  while (available > 0) {
    const size_t wanted =
        min(static_cast<size_t>(available), sizeof(copyBuffer));
    const int received = backendClient.read(copyBuffer, wanted);
    if (received <= 0) {
      return;
    }
    Serial.write(copyBuffer, static_cast<size_t>(received));
    available = backendClient.available();
    yield();
  }
}

static void copyPn532ToSocket() {
  if (!backendClient || !backendClient.connected()) {
    // Drain responses when nobody owns the reader so stale frames cannot leak
    // into the next connection.
    while (Serial.available()) {
      Serial.read();
    }
    return;
  }

  int available = Serial.available();
  while (available > 0) {
    const size_t wanted =
        min(static_cast<size_t>(available), sizeof(copyBuffer));
    const size_t received = Serial.readBytes(copyBuffer, wanted);
    if (received == 0) {
      return;
    }
    backendClient.write(copyBuffer, received);
    available = Serial.available();
    yield();
  }
}

static void printDiagnosticsIfNeeded() {
#if SERIAL_DIAGNOSTICS
  const uint32_t now = millis();
  if (lastDiagnosticAt != 0 && now - lastDiagnosticAt < 2000) {
    return;
  }
  lastDiagnosticAt = now;

  Serial.print("[bridge] Wi-Fi: ");
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("connecting...");
    return;
  }

  Serial.print("connected, IP=");
  Serial.print(WiFi.localIP());
  Serial.print(", RSSI=");
  Serial.print(WiFi.RSSI());
  Serial.print(" dBm, TCP=");
  Serial.print(BRIDGE_TCP_PORT);
  Serial.print(", host=");
  Serial.print(BRIDGE_NAME);
  Serial.println(".local");
#endif
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);

  // The only UART is dedicated to PN532 HSU. Do not print debug output here.
  Serial.begin(PN532_BAUD_RATE, SERIAL_8N1);
  Serial.setTimeout(2);

#if SERIAL_DIAGNOSTICS
  delay(500);
  Serial.println();
  Serial.println("[bridge] ESP8266 Home Key diagnostic build");
#endif

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  // Modem sleep can defer TCP traffic long enough to miss nfcpy's short PN532
  // acknowledgement windows. A mains-powered door reader favours latency.
  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  WiFi.setAutoReconnect(true);
  connectWifiIfNeeded();
}

void loop() {
  updateStatusLed();

  if (WiFi.status() != WL_CONNECTED) {
    if (backendClient) {
      backendClient.stop();
    }
    mdnsStarted = false;
    connectWifiIfNeeded();
    delay(1);
    return;
  }

  startNetworkServicesIfNeeded();

#if SERIAL_DIAGNOSTICS
  printDiagnosticsIfNeeded();
  if (mdnsStarted) {
    MDNS.update();
  }
  delay(1);
  return;
#endif

  acceptBackend();

  if (backendClient && !backendClient.connected()) {
    backendClient.stop();
  }

  copySocketToPn532();
  copyPn532ToSocket();

  if (mdnsStarted) {
    MDNS.update();
  }
  yield();
}
