#include <Arduino.h>
#include <ArduinoOTA.h>
#include <Crypto.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266httpUpdate.h>
#include <ESP8266WiFi.h>
#include <WebSocketsClient.h>

#include "bridge_config.h"
#include "protocol.h"
#include "secrets.h"

#ifndef SERIAL_DIAGNOSTICS
#define SERIAL_DIAGNOSTICS 0
#endif

WebSocketsClient webSocket;

uint8_t protocolBuffer[PROTOCOL_HEADER_SIZE + MAX_PROTOCOL_PAYLOAD];
uint8_t pn532Ring[PN532_RING_SIZE];
// Keep the large PN532 exchange buffers in static storage. Allocating both
// inside the WebSocket callback can exhaust the ESP8266's callback stack when
// nfcpy performs its 275-byte PN532 line-diagnostic exchange.
uint8_t pn532AckFrame[MAX_PROTOCOL_PAYLOAD];
uint8_t pn532ResponseFrame[MAX_PROTOCOL_PAYLOAD];
size_t ringHead = 0;
size_t ringTail = 0;
bool waitingForPn532Response = false;
bool websocketConnected = false;
bool otaStarted = false;
bool otaInProgress = false;
bool buttonPending = false;
bool buttonRawPressed = false;
bool buttonStablePressed = false;
bool firmwareCheckRequested = false;
bool firmwareFirstCheckCompleted = false;
uint32_t lastWifiAttemptAt = 0;
uint32_t accessFeedbackUntil = 0;
uint32_t buttonChangedAt = 0;
uint32_t buttonRequestId = 0;
uint32_t buttonSentAt = 0;
uint32_t nextButtonRequestId = 0x80000000;
uint32_t lastFirmwareCheckAt = 0;
bool lastAccessGranted = false;
String readerId;
String readerToken;
String deviceHostname;

static void setStatusLed(bool on);

static bool ringEmpty() {
  return ringHead == ringTail;
}

static size_t ringNext(size_t position) {
  return (position + 1) % PN532_RING_SIZE;
}

static void ringClear() {
  ringHead = 0;
  ringTail = 0;
}

static bool ringPush(uint8_t value) {
  const size_t next = ringNext(ringHead);
  if (next == ringTail) {
    return false;
  }
  pn532Ring[ringHead] = value;
  ringHead = next;
  return true;
}

static bool ringPop(uint8_t& value) {
  if (ringEmpty()) {
    return false;
  }
  value = pn532Ring[ringTail];
  ringTail = ringNext(ringTail);
  return true;
}

static void drainPn532Uart() {
  while (Serial.available()) {
    const uint8_t value = static_cast<uint8_t>(Serial.read());
    if (waitingForPn532Response) {
      ringPush(value);
    }
  }
}

static void clearPn532Input() {
  ringClear();
  while (Serial.available()) {
    Serial.read();
  }
}

static bool readByteUntil(uint8_t& value, uint32_t deadline) {
  while (static_cast<int32_t>(deadline - millis()) > 0) {
    if (ringPop(value)) {
      return true;
    }
    if (Serial.available()) {
      value = static_cast<uint8_t>(Serial.read());
      return true;
    }
    yield();
  }
  return false;
}

static bool readExact(
    uint8_t* destination,
    size_t count,
    size_t& position,
    uint32_t deadline
) {
  while (count--) {
    if (position >= MAX_PROTOCOL_PAYLOAD ||
        !readByteUntil(destination[position], deadline)) {
      return false;
    }
    position++;
    // Long extended frames can otherwise monopolise the ESP8266 cooperative
    // scheduler while bytes are continuously available.
    yield();
  }
  return true;
}

static bool validateNormalFrame(const uint8_t* frame, size_t length) {
  if (length < 8) {
    return false;
  }
  const uint8_t dataLength = frame[3];
  if (static_cast<uint8_t>(dataLength + frame[4]) != 0) {
    return false;
  }
  uint8_t checksum = 0;
  for (size_t i = 5; i < 5 + static_cast<size_t>(dataLength); i++) {
    checksum = static_cast<uint8_t>(checksum + frame[i]);
  }
  checksum = static_cast<uint8_t>(checksum + frame[5 + dataLength]);
  return checksum == 0 && frame[6 + dataLength] == 0;
}

static bool readPn532Frame(
    uint16_t timeoutMs,
    uint8_t* output,
    size_t& outputLength,
    ErrorCode& error
) {
  const uint32_t deadline = millis() + max<uint16_t>(timeoutMs, 50);
  outputLength = 0;

  // Find the PN532 00 00 FF start code while tolerating extra zero preamble.
  uint8_t syncState = 0;
  uint8_t value = 0;
  while (static_cast<int32_t>(deadline - millis()) > 0) {
    if (!readByteUntil(value, deadline)) {
      error = ErrorCode::PN532_TIMEOUT;
      return false;
    }
    if (syncState == 0) {
      syncState = value == 0x00 ? 1 : 0;
    } else if (syncState == 1) {
      syncState = value == 0x00 ? 2 : 0;
    } else if (value == 0xFF) {
      output[0] = 0x00;
      output[1] = 0x00;
      output[2] = 0xFF;
      outputLength = 3;
      break;
    } else {
      syncState = value == 0x00 ? 2 : 0;
    }
  }

  if (outputLength == 0 ||
      !readExact(output, 3, outputLength, deadline)) {
    error = ErrorCode::PN532_TIMEOUT;
    return false;
  }

  // ACK: 00 00 FF 00 FF 00
  if (output[3] == 0x00 && output[4] == 0xFF && output[5] == 0x00) {
    return true;
  }

  if (output[3] == 0xFF && output[4] == 0xFF) {
    // Extended frame: the first length byte is already at output[5].
    if (!readExact(output, 3, outputLength, deadline)) {
      error = ErrorCode::PN532_TIMEOUT;
      return false;
    }
    const size_t dataLength =
        (static_cast<size_t>(output[5]) << 8) | output[6];
    if (dataLength + 10 > MAX_PROTOCOL_PAYLOAD) {
      error = ErrorCode::PN532_FRAME_TOO_LARGE;
      return false;
    }
    if (!readExact(output, dataLength + 1, outputLength, deadline)) {
      error = ErrorCode::PN532_TIMEOUT;
      return false;
    }
    return true;
  }

  const size_t dataLength = output[3];
  if (dataLength + 7 > MAX_PROTOCOL_PAYLOAD) {
    error = ErrorCode::PN532_FRAME_TOO_LARGE;
    return false;
  }
  if (!readExact(output, dataLength + 1, outputLength, deadline)) {
    error = ErrorCode::PN532_TIMEOUT;
    return false;
  }
  if (!validateNormalFrame(output, outputLength)) {
    error = ErrorCode::PN532_BAD_FRAME;
    return false;
  }
  return true;
}

static void sendProtocolMessage(
    MessageType type,
    uint32_t requestId,
    uint16_t timeoutMs,
    const uint8_t* payload,
    uint16_t payloadLength
) {
  protocolBuffer[0] = PROTOCOL_MAGIC_0;
  protocolBuffer[1] = PROTOCOL_MAGIC_1;
  protocolBuffer[2] = PROTOCOL_VERSION;
  protocolBuffer[3] = static_cast<uint8_t>(type);
  writeU32(protocolBuffer + 4, requestId);
  writeU16(protocolBuffer + 8, timeoutMs);
  writeU16(protocolBuffer + 10, payloadLength);
  if (payloadLength && payload != nullptr) {
    memcpy(protocolBuffer + PROTOCOL_HEADER_SIZE, payload, payloadLength);
  }
  webSocket.sendBIN(protocolBuffer, PROTOCOL_HEADER_SIZE + payloadLength);
}

static void sendError(uint32_t requestId, ErrorCode error) {
  const uint8_t code = static_cast<uint8_t>(error);
  sendProtocolMessage(
      MessageType::ERROR_RESPONSE, requestId, 0, &code, sizeof(code)
  );
}

static void handleExecute(const ProtocolMessage& message) {
  if (message.payloadLength == 0 || waitingForPn532Response) {
    sendError(message.requestId, ErrorCode::PN532_BUSY);
    return;
  }

  clearPn532Input();
  Serial.write(message.payload, message.payloadLength);
  Serial.flush();

  size_t frameLength = 0;
  ErrorCode error = ErrorCode::PN532_TIMEOUT;
  if (!readPn532Frame(
          message.timeoutMs,
          pn532AckFrame,
          frameLength,
          error
      )) {
    sendError(message.requestId, error);
    return;
  }

  static const uint8_t ACK[] = {0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00};
  if (frameLength != sizeof(ACK) ||
      memcmp(pn532AckFrame, ACK, sizeof(ACK)) != 0) {
    sendError(message.requestId, ErrorCode::PN532_BAD_ACK);
    return;
  }

  // Keep the PN532 acknowledgement and response entirely local. Returning
  // only the ACK would force a second WebSocket request before the response
  // can be consumed, adding a full LAN round trip inside a time-sensitive
  // ISO-DEP session.
  waitingForPn532Response = true;
  size_t responseLength = 0;
  if (!readPn532Frame(
          PN532_LOCAL_RESPONSE_TIMEOUT_MS,
          pn532ResponseFrame,
          responseLength,
          error
      )) {
    waitingForPn532Response = false;
    clearPn532Input();
    sendError(message.requestId, error);
    return;
  }
  waitingForPn532Response = false;

  if (frameLength + responseLength > MAX_PROTOCOL_PAYLOAD) {
    sendError(message.requestId, ErrorCode::PN532_FRAME_TOO_LARGE);
    return;
  }
  memcpy(
      pn532AckFrame + frameLength,
      pn532ResponseFrame,
      responseLength
  );
  frameLength += responseLength;
  sendProtocolMessage(
      MessageType::RESPONSE,
      message.requestId,
      0,
      pn532AckFrame,
      static_cast<uint16_t>(frameLength)
  );
}

static void handleReadFrame(const ProtocolMessage& message) {
  // Kept in the protocol enum so older backends fail explicitly after a
  // firmware upgrade. v2 transports receive ACK + response from EXECUTE.
  sendError(message.requestId, ErrorCode::UNSUPPORTED_TYPE);
}

static void handleAccessResult(const ProtocolMessage& message) {
  // Payload: granted (u8), unlock duration (u32 big-endian). The access
  // controller performs the actual door action; this is local feedback only.
  if (message.payloadLength != 5 || message.payload[0] > 1) {
    sendError(message.requestId, ErrorCode::BAD_MESSAGE);
    return;
  }
  lastAccessGranted = message.payload[0] == 1;
  accessFeedbackUntil = millis() + ACCESS_FEEDBACK_DURATION_MS;
  sendProtocolMessage(
      MessageType::RESPONSE, message.requestId, 0, nullptr, 0
  );
}

static void handleButtonResult(const ProtocolMessage& message) {
  if (!buttonPending ||
      message.requestId != buttonRequestId ||
      message.payloadLength != 1 ||
      message.payload[0] > 1) {
    return;
  }
  buttonPending = false;
  lastAccessGranted = message.payload[0] == 1;
  accessFeedbackUntil = millis() + (
      lastAccessGranted
          ? BUTTON_SUCCESS_DURATION_MS
          : BUTTON_FAILURE_DURATION_MS
  );
}

static void handleFirmwareUpdateCheck(const ProtocolMessage& message) {
  if (message.payloadLength != 0) {
    sendError(message.requestId, ErrorCode::BAD_MESSAGE);
    return;
  }
  sendProtocolMessage(
      MessageType::RESPONSE, message.requestId, 0, nullptr, 0
  );
  firmwareCheckRequested = true;
}

static void handleBinaryMessage(uint8_t* payload, size_t length) {
  ProtocolMessage message{};
  if (!decodeMessage(payload, length, message)) {
    sendError(0, ErrorCode::BAD_MESSAGE);
    return;
  }

  switch (message.type) {
    case MessageType::EXECUTE:
      handleExecute(message);
      break;
    case MessageType::READ_FRAME:
      handleReadFrame(message);
      break;
    case MessageType::RESET:
      waitingForPn532Response = false;
      clearPn532Input();
      sendProtocolMessage(
          MessageType::RESPONSE, message.requestId, 0, nullptr, 0
      );
      break;
    case MessageType::ACCESS_RESULT:
      handleAccessResult(message);
      break;
    case MessageType::BUTTON_RESULT:
      handleButtonResult(message);
      break;
    case MessageType::FIRMWARE_UPDATE_CHECK:
      handleFirmwareUpdateCheck(message);
      break;
    default:
      sendError(message.requestId, ErrorCode::UNSUPPORTED_TYPE);
      break;
  }
}

static String normalizedMacAddress() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char value[13];
  snprintf(
      value,
      sizeof(value),
      "%02x%02x%02x%02x%02x%02x",
      mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]
  );
  return String(value);
}

static String deriveReaderToken() {
  String token = experimental::crypto::SHA256::hmac(
      readerId,
      FLEET_SECRET,
      strlen(FLEET_SECRET),
      experimental::crypto::SHA256::NATURAL_LENGTH
  );
  token.toLowerCase();
  return token;
}

static void sendHello() {
  String hello = "{\"type\":\"hello\",\"protocol\":1,\"reader_id\":\"";
  hello += readerId;
  hello += "\",\"token\":\"";
  hello += readerToken;
  hello += "\",\"firmware\":\"";
  hello += FIRMWARE_VERSION;
  hello += "\"}";
  webSocket.sendTXT(hello);
}

static void webSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      websocketConnected = true;
      waitingForPn532Response = false;
      clearPn532Input();
      sendHello();
      break;
    case WStype_DISCONNECTED:
      websocketConnected = false;
      waitingForPn532Response = false;
      if (buttonPending) {
        buttonPending = false;
        lastAccessGranted = false;
        accessFeedbackUntil =
            millis() + BUTTON_FAILURE_DURATION_MS;
      }
      clearPn532Input();
      break;
    case WStype_BIN:
      handleBinaryMessage(payload, length);
      break;
    default:
      break;
  }
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
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

static void startOtaIfNeeded() {
  if (otaStarted || WiFi.status() != WL_CONNECTED) {
    return;
  }
  ArduinoOTA.setHostname(deviceHostname.c_str());
  ArduinoOTA.setPassword(readerToken.c_str());
  ArduinoOTA.setRebootOnSuccess(true);
  ArduinoOTA.onStart([]() {
    otaInProgress = true;
    websocketConnected = false;
    waitingForPn532Response = false;
    clearPn532Input();
    webSocket.disconnect();
  });
  ArduinoOTA.onEnd([]() {
    digitalWrite(STATUS_LED_PIN, LOW);
  });
  ArduinoOTA.onError([](ota_error_t) {
    otaInProgress = false;
  });
  ArduinoOTA.begin(true);
  otaStarted = true;
}

static void configureHttpUpdates() {
  ESPhttpUpdate.rebootOnUpdate(true);
  ESPhttpUpdate.closeConnectionsOnUpdate(true);
  ESPhttpUpdate.setClientTimeout(5000);
  ESPhttpUpdate.setAuthorization(readerId, readerToken);
  ESPhttpUpdate.onStart([]() {
    otaInProgress = true;
    websocketConnected = false;
    waitingForPn532Response = false;
    buttonPending = false;
    clearPn532Input();
    webSocket.disconnect();
  });
  ESPhttpUpdate.onEnd([]() {
    setStatusLed(false);
  });
  ESPhttpUpdate.onError([](int) {
    otaInProgress = false;
  });
}

static void checkForFirmwareUpdate() {
  if (WiFi.status() != WL_CONNECTED || !websocketConnected ||
      otaInProgress || waitingForPn532Response || buttonPending) {
    return;
  }
  const bool requested = firmwareCheckRequested;
  firmwareCheckRequested = false;
  firmwareFirstCheckCompleted = true;
  lastFirmwareCheckAt = millis();

  // Stop the controller from starting another PN532 operation while the
  // firmware request is in progress. A 304 reconnects normally; a 200 enters
  // the updater and reboots only after the image has been verified.
  websocketConnected = false;
  webSocket.disconnect();
  yield();

  WiFiClient client;
  const HTTPUpdateResult result = ESPhttpUpdate.update(
      client,
      BACKEND_HOST,
      FIRMWARE_API_PORT,
      FIRMWARE_UPDATE_PATH,
      FIRMWARE_VERSION
  );
  if (result == HTTP_UPDATE_FAILED && requested) {
    lastAccessGranted = false;
    accessFeedbackUntil = millis() + BUTTON_FAILURE_DURATION_MS;
  }
}

static void checkFirmwareUpdateIfDue() {
  const uint32_t now = millis();
  const bool requested = firmwareCheckRequested;
  const bool initial = (
      !firmwareFirstCheckCompleted &&
      now >= FIRMWARE_INITIAL_CHECK_DELAY_MS
  );
  const bool periodic = (
      firmwareFirstCheckCompleted &&
      now - lastFirmwareCheckAt >= FIRMWARE_CHECK_INTERVAL_MS
  );
  if (requested || initial || periodic) {
    checkForFirmwareUpdate();
  }
}

static void updateButton() {
  const uint32_t now = millis();
  const bool pressed = digitalRead(BUTTON_PIN) == LOW;
  if (pressed != buttonRawPressed) {
    buttonRawPressed = pressed;
    buttonChangedAt = now;
  }
  if (now - buttonChangedAt >= BUTTON_DEBOUNCE_MS &&
      buttonStablePressed != buttonRawPressed) {
    buttonStablePressed = buttonRawPressed;
    if (buttonStablePressed && !buttonPending) {
      if (!websocketConnected) {
        lastAccessGranted = false;
        accessFeedbackUntil =
            now + BUTTON_FAILURE_DURATION_MS;
      } else {
        buttonRequestId = nextButtonRequestId++;
        if (nextButtonRequestId == 0) {
          nextButtonRequestId = 0x80000000;
        }
        buttonPending = true;
        buttonSentAt = now;
        sendProtocolMessage(
            MessageType::BUTTON_EVENT,
            buttonRequestId,
            0,
            nullptr,
            0
        );
      }
    }
  }
  if (buttonPending &&
      now - buttonSentAt >= BUTTON_RESULT_TIMEOUT_MS) {
    buttonPending = false;
    lastAccessGranted = false;
    accessFeedbackUntil = now + BUTTON_FAILURE_DURATION_MS;
  }
}

static void setStatusLed(bool on) {
  digitalWrite(STATUS_LED_PIN, on ? HIGH : LOW);
}

static void updateStatusLed() {
  if (otaInProgress) {
    setStatusLed((millis() / 50) % 2);
    return;
  }
  if (buttonPending) {
    setStatusLed(true);
    return;
  }
  if (static_cast<int32_t>(accessFeedbackUntil - millis()) > 0) {
    // Success holds the LED on; failure uses a rapid attention pattern.
    setStatusLed(
        lastAccessGranted || ((millis() / 70) % 2)
    );
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    setStatusLed((millis() / 250) % 2);
  } else if (websocketConnected) {
    setStatusLed(false);
  } else {
    setStatusLed(millis() % 2000 < 100);
  }
}

void setup() {
  pinMode(STATUS_LED_PIN, OUTPUT);
  setStatusLed(false);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  buttonRawPressed = digitalRead(BUTTON_PIN) == LOW;
  buttonStablePressed = buttonRawPressed;

  Serial.setRxBufferSize(PN532_RING_SIZE);
  Serial.begin(PN532_BAUD_RATE, SERIAL_8N1);
  Serial.setTimeout(2);

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  WiFi.setAutoReconnect(true);

  readerId = normalizedMacAddress();
  readerToken = deriveReaderToken();
  deviceHostname = "TDS-Door-Access-V2-" + readerId;
  configureHttpUpdates();
  WiFi.hostname(deviceHostname);
  connectWifiIfNeeded();

  webSocket.begin(BACKEND_HOST, BACKEND_PORT, BACKEND_PATH);
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(WS_RECONNECT_INTERVAL_MS);
  webSocket.enableHeartbeat(
      WS_HEARTBEAT_INTERVAL_MS,
      WS_HEARTBEAT_TIMEOUT_MS,
      WS_HEARTBEAT_MISSES
  );
}

void loop() {
  connectWifiIfNeeded();
  startOtaIfNeeded();
  if (otaStarted) {
    ArduinoOTA.handle();
  }
  if (otaInProgress) {
    updateStatusLed();
    yield();
    return;
  }
  webSocket.loop();
  updateButton();
  updateStatusLed();
  drainPn532Uart();
  checkFirmwareUpdateIfDue();

#if SERIAL_DIAGNOSTICS
  // Diagnostic builds should be used with PN532 TX/RX disconnected.
  static uint32_t lastStatusAt = 0;
  if (millis() - lastStatusAt >= 2000) {
    lastStatusAt = millis();
    Serial.printf(
        "[bridge] id=%s host=%s wifi=%s ip=%s websocket=%s ota=%s\n",
        readerId.c_str(),
        deviceHostname.c_str(),
        WiFi.status() == WL_CONNECTED ? "connected" : "connecting",
        WiFi.localIP().toString().c_str(),
        websocketConnected ? "connected" : "connecting",
        otaStarted ? "ready" : "starting"
    );
  }
#endif

  yield();
}
