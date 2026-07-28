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
bool autonomousDiscoveryEnabled = false;
bool discoveryTargetPending = false;
bool discoveryWaitForRemoval = false;
uint8_t discoveryFrame[64];
uint8_t discoveryFrameLength = 0;
uint8_t consecutiveDiscoveryFailures = 0;
uint32_t nextWifiAttemptAt = 0;
uint32_t wifiConnectedAt = 0;
uint32_t wifiReconnectDelayMs = WIFI_RETRY_INTERVAL_MS;
uint32_t wifiDisconnectCount = 0;
uint32_t websocketConnectedAt = 0;
uint32_t websocketDisconnectedAt = 0;
uint32_t lastWifiPathRecoveryAt = 0;
uint32_t websocketReconnectDelayMs = WS_RECONNECT_INTERVAL_MS;
uint32_t accessFeedbackUntil = 0;
uint32_t buttonChangedAt = 0;
uint32_t buttonRequestId = 0;
uint32_t buttonSentAt = 0;
uint32_t nextButtonRequestId = 0x80000000;
uint32_t lastFirmwareCheckAt = 0;
uint32_t nextDiscoveryAt = 0;
uint32_t discoveryTargetPendingAt = 0;
uint32_t discoveryNoTargetSince = 0;
uint32_t nextTargetEventRequestId = 0x90000000;
uint32_t nextReaderStatusRequestId = 0xA0000000;
bool lastAccessGranted = false;
bool wifiStarted = false;
bool wifiWasConnected = false;
String readerId;
String readerToken;
String deviceHostname;

enum class AsyncFramePhase : uint8_t {
  IDLE,
  WAIT_ACK,
  WAIT_RESPONSE,
};

enum class TransceiveStep : uint8_t {
  IDLE,
  READ_REGISTERS,
  WRITE_REGISTERS,
  CONFIGURE_TIMEOUT,
  EXCHANGE,
};

struct AsyncPn532Command {
  bool active = false;
  AsyncFramePhase phase = AsyncFramePhase::IDLE;
  uint8_t command = 0;
  uint8_t syncState = 0;
  size_t frameLength = 0;
  uint16_t responseTimeoutMs = 0;
  uint32_t deadline = 0;
};

AsyncPn532Command asyncPn532;
TransceiveStep transceiveStep = TransceiveStep::IDLE;
uint8_t transceivePayload[250];
uint8_t transceivePayloadLength = 0;
uint16_t transceiveTimeoutMs = 0;
uint32_t transceiveRequestId = 0;

static void setStatusLed(bool on);
static void setNetworkLed(bool on);

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

static void cancelAsyncTransceive() {
  asyncPn532.active = false;
  asyncPn532.phase = AsyncFramePhase::IDLE;
  asyncPn532.frameLength = 0;
  asyncPn532.syncState = 0;
  transceiveStep = TransceiveStep::IDLE;
  transceivePayloadLength = 0;
  waitingForPn532Response = false;
}

static void hardwareResetPn532() {
  cancelAsyncTransceive();
  autonomousDiscoveryEnabled = false;
  discoveryTargetPending = false;
  discoveryWaitForRemoval = false;
  consecutiveDiscoveryFailures = 0;
  clearPn532Input();
  digitalWrite(PN532_RESET_PIN, LOW);
  delay(PN532_RESET_ASSERT_MS);
  digitalWrite(PN532_RESET_PIN, HIGH);
  delay(PN532_RESET_BOOT_MS);
  clearPn532Input();
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

static bool executeLocalPn532Command(
    uint8_t command,
    const uint8_t* commandData,
    uint8_t commandDataLength,
    uint16_t responseTimeoutMs,
    uint8_t* responseData,
    size_t& responseDataLength,
    ErrorCode& error
) {
  const uint8_t frameDataLength =
      static_cast<uint8_t>(commandDataLength + 2);
  size_t frameLength = 0;
  protocolBuffer[frameLength++] = 0x00;
  protocolBuffer[frameLength++] = 0x00;
  protocolBuffer[frameLength++] = 0xFF;
  protocolBuffer[frameLength++] = frameDataLength;
  protocolBuffer[frameLength++] =
      static_cast<uint8_t>(0 - frameDataLength);
  protocolBuffer[frameLength++] = 0xD4;
  protocolBuffer[frameLength++] = command;
  uint8_t checksum = static_cast<uint8_t>(0xD4 + command);
  for (uint8_t i = 0; i < commandDataLength; i++) {
    protocolBuffer[frameLength++] = commandData[i];
    checksum = static_cast<uint8_t>(checksum + commandData[i]);
  }
  protocolBuffer[frameLength++] = static_cast<uint8_t>(0 - checksum);
  protocolBuffer[frameLength++] = 0x00;

  clearPn532Input();
  Serial.write(protocolBuffer, frameLength);
  Serial.flush();

  size_t acknowledgementLength = 0;
  if (!readPn532Frame(
          250,
          pn532AckFrame,
          acknowledgementLength,
          error
      )) {
    return false;
  }
  static const uint8_t ACK[] = {0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00};
  if (acknowledgementLength != sizeof(ACK) ||
      memcmp(pn532AckFrame, ACK, sizeof(ACK)) != 0) {
    error = ErrorCode::PN532_BAD_ACK;
    return false;
  }

  waitingForPn532Response = true;
  size_t responseFrameLength = 0;
  const bool received = readPn532Frame(
      responseTimeoutMs,
      pn532ResponseFrame,
      responseFrameLength,
      error
  );
  waitingForPn532Response = false;
  if (!received) {
    clearPn532Input();
    return false;
  }

  // Atomic discovery commands and their responses are always normal frames.
  if (responseFrameLength < 9 ||
      pn532ResponseFrame[0] != 0x00 ||
      pn532ResponseFrame[1] != 0x00 ||
      pn532ResponseFrame[2] != 0xFF) {
    error = ErrorCode::PN532_BAD_FRAME;
    return false;
  }
  const size_t dataLength = pn532ResponseFrame[3];
  if (dataLength < 2 ||
      dataLength + 7 != responseFrameLength ||
      pn532ResponseFrame[5] != 0xD5 ||
      pn532ResponseFrame[6] != static_cast<uint8_t>(command + 1)) {
    error = ErrorCode::PN532_BAD_FRAME;
    return false;
  }
  responseDataLength = dataLength - 2;
  if (responseDataLength > MAX_PROTOCOL_PAYLOAD) {
    error = ErrorCode::PN532_FRAME_TOO_LARGE;
    return false;
  }
  if (responseDataLength > 0) {
    memcpy(responseData, pn532ResponseFrame + 7, responseDataLength);
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

static void handleDiscover(const ProtocolMessage& message) {
  // Payload is one complete Home Key ECP frame including CRC-A. Keeping the
  // entire PN532 sequence here removes six LAN round trips per discovery
  // cycle while authentication remains under controller ownership.
  if (message.payloadLength < 3 || message.payloadLength > 64 ||
      waitingForPn532Response) {
    sendError(
        message.requestId,
        waitingForPn532Response
            ? ErrorCode::PN532_BUSY
            : ErrorCode::BAD_MESSAGE
    );
    return;
  }

  static const uint8_t RF_FIELD_OFF[] = {0x01, 0x02};
  static const uint8_t POLL_TYPE_A[] = {0x01, 0x00};
  static const uint8_t DETECTION_RETRIES[] = {
      0x05, 0xFF, 0x01, 0x00
  };
  static const uint8_t RF_TIMEOUTS[] = {0x02, 0x0A, 0x0B, 0x08};
  static const uint8_t BIT_FRAMING[] = {0x63, 0x3D, 0x00};

  ErrorCode error = ErrorCode::PN532_TIMEOUT;
  size_t responseLength = 0;
  auto execute = [&](
      uint8_t command,
      const uint8_t* data,
      uint8_t dataLength,
      uint16_t timeoutMs = PN532_LOCAL_RESPONSE_TIMEOUT_MS
  ) {
    responseLength = 0;
    return executeLocalPn532Command(
        command,
        data,
        dataLength,
        timeoutMs,
        pn532AckFrame,
        responseLength,
        error
    );
  };
  auto sendTarget = [&]() {
    if (responseLength + 1 > MAX_PROTOCOL_PAYLOAD) {
      sendError(message.requestId, ErrorCode::PN532_FRAME_TOO_LARGE);
      return;
    }
    memmove(pn532AckFrame + 1, pn532AckFrame, responseLength);
    pn532AckFrame[0] = 1;
    sendProtocolMessage(
        MessageType::RESPONSE,
        message.requestId,
        0,
        pn532AckFrame,
        static_cast<uint16_t>(responseLength + 1)
    );
  };

  if (!execute(
          0x32,
          RF_FIELD_OFF,
          sizeof(RF_FIELD_OFF),
          300
      ) ||
      !execute(0x4A, POLL_TYPE_A, sizeof(POLL_TYPE_A), 500)) {
    sendError(message.requestId, error);
    return;
  }
  if (responseLength > 0 && pn532AckFrame[0] > 0) {
    sendTarget();
    return;
  }

  if (!execute(
          0x32,
          DETECTION_RETRIES,
          sizeof(DETECTION_RETRIES),
          300
      ) ||
      !execute(0x32, RF_TIMEOUTS, sizeof(RF_TIMEOUTS), 300) ||
      !execute(0x08, BIT_FRAMING, sizeof(BIT_FRAMING), 300) ||
      !execute(
          0x42,
          message.payload,
          static_cast<uint8_t>(message.payloadLength),
          300
      )) {
    sendError(message.requestId, error);
    return;
  }

  // End the ECP cycle with the field off and return immediately. The phone
  // needs this field-off interval to transition into Home Key mode. Polling
  // again inside this request both removed that interval and could block for
  // another PN532 target-search timeout.
  if (!execute(0x32, RF_FIELD_OFF, sizeof(RF_FIELD_OFF), 300)) {
    sendError(message.requestId, error);
    return;
  }

  const uint8_t noTarget = 0;
  sendProtocolMessage(
      MessageType::RESPONSE,
      message.requestId,
      0,
      &noTarget,
      sizeof(noTarget)
  );
}

static void handleStartDiscovery(const ProtocolMessage& message) {
  if (message.payloadLength < 3 ||
      message.payloadLength > sizeof(discoveryFrame) ||
      waitingForPn532Response) {
    sendError(
        message.requestId,
        waitingForPn532Response
            ? ErrorCode::PN532_BUSY
            : ErrorCode::BAD_MESSAGE
    );
    return;
  }
  memcpy(discoveryFrame, message.payload, message.payloadLength);
  discoveryFrameLength = static_cast<uint8_t>(message.payloadLength);
  autonomousDiscoveryEnabled = true;
  discoveryTargetPending = false;
  discoveryWaitForRemoval = false;
  consecutiveDiscoveryFailures = 0;
  nextDiscoveryAt = millis();
  sendProtocolMessage(
      MessageType::RESPONSE, message.requestId, 0, nullptr, 0
  );
  const uint8_t status[] = {
      static_cast<uint8_t>(ReaderRuntimeState::READY),
      0,
      0,
  };
  sendProtocolMessage(
      MessageType::READER_STATUS,
      nextReaderStatusRequestId++,
      0,
      status,
      sizeof(status)
  );
}

static void handleResumeDiscovery(const ProtocolMessage& message) {
  if (message.payloadLength > 1 ||
      (message.payloadLength == 1 && message.payload[0] > 1) ||
      !autonomousDiscoveryEnabled) {
    sendError(message.requestId, ErrorCode::BAD_MESSAGE);
    return;
  }
  const bool retry = (
      message.payloadLength == 1 && message.payload[0] == 1
  );
  if (retry) {
    static const uint8_t RF_FIELD_OFF[] = {0x01, 0x02};
    ErrorCode error = ErrorCode::PN532_TIMEOUT;
    size_t responseLength = 0;
    if (!executeLocalPn532Command(
            0x32,
            RF_FIELD_OFF,
            sizeof(RF_FIELD_OFF),
            300,
            pn532AckFrame,
            responseLength,
            error
        )) {
      sendError(message.requestId, error);
      return;
    }
  }
  discoveryTargetPending = false;
  discoveryWaitForRemoval = !retry;
  discoveryNoTargetSince = 0;
  consecutiveDiscoveryFailures = 0;
  nextDiscoveryAt = millis() + (
      retry ? DISCOVERY_RETRY_DELAY_MS : DISCOVERY_INTERVAL_MS
  );
  sendProtocolMessage(
      MessageType::RESPONSE, message.requestId, 0, nullptr, 0
  );
}

enum class AsyncCommandResult : uint8_t {
  PENDING,
  COMPLETE,
  FAILED,
};

static void resetAsyncFrameCollector() {
  asyncPn532.frameLength = 0;
  asyncPn532.syncState = 0;
}

static AsyncCommandResult collectAsyncPn532Frame(ErrorCode& error) {
  uint8_t value = 0;
  while (ringPop(value)) {
    if (asyncPn532.frameLength == 0) {
      if (asyncPn532.syncState == 0) {
        asyncPn532.syncState = value == 0x00 ? 1 : 0;
      } else if (asyncPn532.syncState == 1) {
        asyncPn532.syncState = value == 0x00 ? 2 : 0;
      } else if (value == 0xFF) {
        pn532ResponseFrame[0] = 0x00;
        pn532ResponseFrame[1] = 0x00;
        pn532ResponseFrame[2] = 0xFF;
        asyncPn532.frameLength = 3;
      } else {
        asyncPn532.syncState = value == 0x00 ? 2 : 0;
      }
      continue;
    }

    if (asyncPn532.frameLength >= MAX_PROTOCOL_PAYLOAD) {
      error = ErrorCode::PN532_FRAME_TOO_LARGE;
      return AsyncCommandResult::FAILED;
    }
    pn532ResponseFrame[asyncPn532.frameLength++] = value;

    if (asyncPn532.frameLength == 6 &&
        pn532ResponseFrame[3] == 0x00 &&
        pn532ResponseFrame[4] == 0xFF &&
        pn532ResponseFrame[5] == 0x00) {
      return AsyncCommandResult::COMPLETE;
    }

    size_t expectedLength = 0;
    if (asyncPn532.frameLength >= 5 &&
        !(pn532ResponseFrame[3] == 0xFF &&
          pn532ResponseFrame[4] == 0xFF)) {
      expectedLength =
          static_cast<size_t>(pn532ResponseFrame[3]) + 7;
    } else if (asyncPn532.frameLength >= 7) {
      const size_t dataLength =
          (static_cast<size_t>(pn532ResponseFrame[5]) << 8) |
          pn532ResponseFrame[6];
      expectedLength = dataLength + 10;
    }

    if (expectedLength > MAX_PROTOCOL_PAYLOAD) {
      error = ErrorCode::PN532_FRAME_TOO_LARGE;
      return AsyncCommandResult::FAILED;
    }
    if (expectedLength != 0 &&
        asyncPn532.frameLength == expectedLength) {
      return AsyncCommandResult::COMPLETE;
    }
  }

  if (static_cast<int32_t>(millis() - asyncPn532.deadline) >= 0) {
    error = ErrorCode::PN532_TIMEOUT;
    return AsyncCommandResult::FAILED;
  }
  return AsyncCommandResult::PENDING;
}

static bool beginAsyncPn532Command(
    uint8_t command,
    const uint8_t* commandData,
    uint8_t commandDataLength,
    uint16_t responseTimeoutMs
) {
  if (asyncPn532.active) {
    return false;
  }

  const uint8_t frameDataLength =
      static_cast<uint8_t>(commandDataLength + 2);
  size_t frameLength = 0;
  protocolBuffer[frameLength++] = 0x00;
  protocolBuffer[frameLength++] = 0x00;
  protocolBuffer[frameLength++] = 0xFF;
  protocolBuffer[frameLength++] = frameDataLength;
  protocolBuffer[frameLength++] =
      static_cast<uint8_t>(0 - frameDataLength);
  protocolBuffer[frameLength++] = 0xD4;
  protocolBuffer[frameLength++] = command;
  uint8_t checksum = static_cast<uint8_t>(0xD4 + command);
  for (uint8_t i = 0; i < commandDataLength; i++) {
    protocolBuffer[frameLength++] = commandData[i];
    checksum = static_cast<uint8_t>(checksum + commandData[i]);
  }
  protocolBuffer[frameLength++] = static_cast<uint8_t>(0 - checksum);
  protocolBuffer[frameLength++] = 0x00;

  clearPn532Input();
  waitingForPn532Response = true;
  asyncPn532.active = true;
  asyncPn532.phase = AsyncFramePhase::WAIT_ACK;
  asyncPn532.command = command;
  asyncPn532.responseTimeoutMs =
      max<uint16_t>(responseTimeoutMs, 50);
  asyncPn532.deadline = millis() + 250;
  resetAsyncFrameCollector();
  Serial.write(protocolBuffer, frameLength);
  Serial.flush();
  return true;
}

static AsyncCommandResult pollAsyncPn532Command(
    size_t& responseLength,
    ErrorCode& error
) {
  responseLength = 0;
  if (!asyncPn532.active) {
    error = ErrorCode::PN532_BAD_FRAME;
    return AsyncCommandResult::FAILED;
  }

  const AsyncCommandResult frameResult =
      collectAsyncPn532Frame(error);
  if (frameResult != AsyncCommandResult::COMPLETE) {
    if (frameResult == AsyncCommandResult::FAILED) {
      // PN532 ACK also aborts a command still waiting inside the chip.
      static const uint8_t ACK[] = {
          0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00
      };
      Serial.write(ACK, sizeof(ACK));
      Serial.flush();
      asyncPn532.active = false;
      clearPn532Input();
    }
    return frameResult;
  }

  if (asyncPn532.phase == AsyncFramePhase::WAIT_ACK) {
    static const uint8_t ACK[] = {
        0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00
    };
    if (asyncPn532.frameLength != sizeof(ACK) ||
        memcmp(pn532ResponseFrame, ACK, sizeof(ACK)) != 0) {
      error = ErrorCode::PN532_BAD_ACK;
      asyncPn532.active = false;
      return AsyncCommandResult::FAILED;
    }
    asyncPn532.phase = AsyncFramePhase::WAIT_RESPONSE;
    asyncPn532.deadline =
        millis() + asyncPn532.responseTimeoutMs;
    resetAsyncFrameCollector();
    return AsyncCommandResult::PENDING;
  }

  if (!validateNormalFrame(
          pn532ResponseFrame, asyncPn532.frameLength
      ) ||
      asyncPn532.frameLength < 9) {
    error = ErrorCode::PN532_BAD_FRAME;
    asyncPn532.active = false;
    return AsyncCommandResult::FAILED;
  }
  const size_t dataLength = pn532ResponseFrame[3];
  if (dataLength < 2 ||
      dataLength + 7 != asyncPn532.frameLength ||
      pn532ResponseFrame[5] != 0xD5 ||
      pn532ResponseFrame[6] !=
          static_cast<uint8_t>(asyncPn532.command + 1)) {
    error = ErrorCode::PN532_BAD_FRAME;
    asyncPn532.active = false;
    return AsyncCommandResult::FAILED;
  }

  responseLength = dataLength - 2;
  if (responseLength > MAX_PROTOCOL_PAYLOAD) {
    error = ErrorCode::PN532_FRAME_TOO_LARGE;
    asyncPn532.active = false;
    return AsyncCommandResult::FAILED;
  }
  if (responseLength > 0) {
    memcpy(
        pn532AckFrame, pn532ResponseFrame + 7, responseLength
    );
  }
  asyncPn532.active = false;
  return AsyncCommandResult::COMPLETE;
}

static void finishAsyncTransceive(ErrorCode error, bool success) {
  const uint32_t requestId = transceiveRequestId;
  cancelAsyncTransceive();
  clearPn532Input();
  if (!success) {
    sendError(requestId, error);
  }
}

static void handleTransceive(const ProtocolMessage& message) {
  if (message.payloadLength == 0 || message.payloadLength > 250 ||
      message.timeoutMs == 0 ||
      waitingForPn532Response || !discoveryTargetPending) {
    sendError(
        message.requestId,
        waitingForPn532Response
            ? ErrorCode::PN532_BUSY
            : ErrorCode::BAD_MESSAGE
    );
    return;
  }

  memcpy(
      transceivePayload, message.payload, message.payloadLength
  );
  transceivePayloadLength =
      static_cast<uint8_t>(message.payloadLength);
  transceiveTimeoutMs = message.timeoutMs;
  transceiveRequestId = message.requestId;
  transceiveStep = TransceiveStep::READ_REGISTERS;

  static const uint8_t READ_RF_REGISTERS[] = {
      0x63, 0x02, 0x63, 0x03, 0x63, 0x05
  };
  if (!beginAsyncPn532Command(
          0x06,
          READ_RF_REGISTERS,
          sizeof(READ_RF_REGISTERS),
          300
      )) {
    finishAsyncTransceive(ErrorCode::PN532_BUSY, false);
  }
}

static void updateAsyncTransceive() {
  if (transceiveStep == TransceiveStep::IDLE) {
    return;
  }

  size_t responseLength = 0;
  ErrorCode error = ErrorCode::PN532_TIMEOUT;
  const AsyncCommandResult result =
      pollAsyncPn532Command(responseLength, error);
  if (result == AsyncCommandResult::PENDING) {
    return;
  }
  if (result == AsyncCommandResult::FAILED) {
    finishAsyncTransceive(error, false);
    return;
  }

  if (transceiveStep == TransceiveStep::READ_REGISTERS) {
    if (responseLength != 3) {
      finishAsyncTransceive(ErrorCode::PN532_BAD_FRAME, false);
      return;
    }
    const uint8_t writeRfRegisters[] = {
        0x63, 0x02, static_cast<uint8_t>(pn532AckFrame[0] & 0x8C),
        0x63, 0x03, static_cast<uint8_t>(pn532AckFrame[1] & 0x8C),
        0x63, 0x05,
        static_cast<uint8_t>((pn532AckFrame[2] & 0xBF) | 0x40),
    };
    transceiveStep = TransceiveStep::WRITE_REGISTERS;
    if (!beginAsyncPn532Command(
            0x08,
            writeRfRegisters,
            sizeof(writeRfRegisters),
            300
        )) {
      finishAsyncTransceive(ErrorCode::PN532_BUSY, false);
    }
    return;
  }

  if (transceiveStep == TransceiveStep::WRITE_REGISTERS) {
    const uint32_t timeoutMicroseconds =
        static_cast<uint32_t>(transceiveTimeoutMs) * 1000UL;
    uint8_t timeoutIndex = 16;
    for (uint8_t index = 0; index < 16; index++) {
      if ((timeoutMicroseconds >> index) <= 100) {
        timeoutIndex = index + 1;
        break;
      }
    }
    const uint8_t rfTimeouts[] = {
        0x02, 0x0A, 0x0B, timeoutIndex
    };
    transceiveStep = TransceiveStep::CONFIGURE_TIMEOUT;
    if (!beginAsyncPn532Command(
            0x32, rfTimeouts, sizeof(rfTimeouts), 300
        )) {
      finishAsyncTransceive(ErrorCode::PN532_BUSY, false);
    }
    return;
  }

  if (transceiveStep == TransceiveStep::CONFIGURE_TIMEOUT) {
    const uint16_t localTimeout = static_cast<uint16_t>(
        min<uint32_t>(
            static_cast<uint32_t>(transceiveTimeoutMs) + 150UL,
            0xFFFFUL
        )
    );
    transceiveStep = TransceiveStep::EXCHANGE;
    if (!beginAsyncPn532Command(
            0x42,
            transceivePayload,
            transceivePayloadLength,
            localTimeout
        )) {
      finishAsyncTransceive(ErrorCode::PN532_BUSY, false);
    }
    return;
  }

  if (responseLength == 0) {
    finishAsyncTransceive(ErrorCode::PN532_BAD_FRAME, false);
    return;
  }
  const uint32_t requestId = transceiveRequestId;
  cancelAsyncTransceive();
  // InCommunicateThru returns status followed by RF response data.
  sendProtocolMessage(
      MessageType::RESPONSE,
      requestId,
      0,
      pn532AckFrame,
      static_cast<uint16_t>(responseLength)
  );
}

static void autonomousDiscoveryFailure(ErrorCode error) {
  consecutiveDiscoveryFailures++;
  nextDiscoveryAt = millis() + 100;
  if (consecutiveDiscoveryFailures < DISCOVERY_FAILURE_LIMIT) {
    return;
  }
  autonomousDiscoveryEnabled = false;
  discoveryTargetPending = false;
  discoveryWaitForRemoval = false;
  // Keep network health independent from PN532 health. The controller will
  // reinitialize the radio over this existing WebSocket with bounded backoff.
  const uint8_t status[] = {
      static_cast<uint8_t>(ReaderRuntimeState::FAILED),
      static_cast<uint8_t>(error),
      consecutiveDiscoveryFailures,
  };
  sendProtocolMessage(
      MessageType::READER_STATUS,
      nextReaderStatusRequestId++,
      0,
      status,
      sizeof(status)
  );
}

static void runAutonomousDiscovery() {
  if (!autonomousDiscoveryEnabled || !websocketConnected ||
      otaInProgress || waitingForPn532Response ||
      discoveryFrameLength == 0) {
    return;
  }

  const uint32_t now = millis();
  if (discoveryTargetPending) {
    if (now - discoveryTargetPendingAt <
        DISCOVERY_TARGET_WATCHDOG_MS) {
      return;
    }
    // The controller disappeared or failed to resume after authentication.
    discoveryTargetPending = false;
    discoveryWaitForRemoval = true;
  }
  if (static_cast<int32_t>(now - nextDiscoveryAt) < 0) {
    return;
  }

  static const uint8_t RF_FIELD_OFF[] = {0x01, 0x02};
  static const uint8_t POLL_TYPE_A[] = {0x01, 0x00};
  static const uint8_t DETECTION_RETRIES[] = {
      0x05, 0xFF, 0x01, 0x00
  };
  static const uint8_t RF_TIMEOUTS[] = {0x02, 0x0A, 0x0B, 0x08};
  static const uint8_t BIT_FRAMING[] = {0x63, 0x3D, 0x00};

  ErrorCode error = ErrorCode::PN532_TIMEOUT;
  size_t responseLength = 0;
  auto execute = [&](
      uint8_t command,
      const uint8_t* data,
      uint8_t dataLength,
      uint16_t timeoutMs
  ) {
    responseLength = 0;
    return executeLocalPn532Command(
        command,
        data,
        dataLength,
        timeoutMs,
        pn532AckFrame,
        responseLength,
        error
    );
  };

  if (!execute(0x32, RF_FIELD_OFF, sizeof(RF_FIELD_OFF), 300) ||
      !execute(0x4A, POLL_TYPE_A, sizeof(POLL_TYPE_A), 500)) {
    autonomousDiscoveryFailure(error);
    return;
  }

  if (responseLength > 0 && pn532AckFrame[0] > 0) {
    consecutiveDiscoveryFailures = 0;
    if (discoveryWaitForRemoval) {
      discoveryNoTargetSince = 0;
      nextDiscoveryAt = millis() + DISCOVERY_INTERVAL_MS;
      return;
    }
    if (responseLength + 1 > MAX_PROTOCOL_PAYLOAD) {
      autonomousDiscoveryFailure(ErrorCode::PN532_FRAME_TOO_LARGE);
      return;
    }
    memmove(pn532AckFrame + 1, pn532AckFrame, responseLength);
    pn532AckFrame[0] = 1;
    const uint32_t requestId = nextTargetEventRequestId++;
    sendProtocolMessage(
        MessageType::TARGET_EVENT,
        requestId,
        0,
        pn532AckFrame,
        static_cast<uint16_t>(responseLength + 1)
    );
    discoveryTargetPending = true;
    discoveryTargetPendingAt = millis();
    return;
  }

  if (discoveryWaitForRemoval) {
    if (discoveryNoTargetSince == 0) {
      discoveryNoTargetSince = millis();
    }
    if (millis() - discoveryNoTargetSince <
        DISCOVERY_REMOVAL_CONFIRM_MS) {
      nextDiscoveryAt = millis() + DISCOVERY_INTERVAL_MS;
      return;
    }
    discoveryWaitForRemoval = false;
    discoveryNoTargetSince = 0;
  }
  if (!execute(
          0x32,
          DETECTION_RETRIES,
          sizeof(DETECTION_RETRIES),
          300
      ) ||
      !execute(0x32, RF_TIMEOUTS, sizeof(RF_TIMEOUTS), 300) ||
      !execute(0x08, BIT_FRAMING, sizeof(BIT_FRAMING), 300) ||
      !execute(
          0x42,
          discoveryFrame,
          discoveryFrameLength,
          300
      ) ||
      !execute(0x32, RF_FIELD_OFF, sizeof(RF_FIELD_OFF), 300)) {
    autonomousDiscoveryFailure(error);
    return;
  }
  consecutiveDiscoveryFailures = 0;
  nextDiscoveryAt = millis() + DISCOVERY_INTERVAL_MS;
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
      hardwareResetPn532();
      sendProtocolMessage(
          MessageType::RESPONSE, message.requestId, 0, nullptr, 0
      );
      break;
    case MessageType::DISCOVER:
      handleDiscover(message);
      break;
    case MessageType::START_DISCOVERY:
      handleStartDiscovery(message);
      break;
    case MessageType::RESUME_DISCOVERY:
      handleResumeDiscovery(message);
      break;
    case MessageType::TRANSCEIVE:
      handleTransceive(message);
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
  hello += "\",\"wifi_rssi\":";
  hello += String(WiFi.RSSI());
  hello += ",\"wifi_reconnects\":";
  hello += String(wifiDisconnectCount);
  hello += "}";
  webSocket.sendTXT(hello);
}

static void webSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      websocketConnected = true;
      websocketConnectedAt = millis();
      websocketDisconnectedAt = 0;
      cancelAsyncTransceive();
      clearPn532Input();
      sendHello();
      break;
    case WStype_DISCONNECTED:
      if (
          !websocketConnected ||
          millis() - websocketConnectedAt < WS_RECONNECT_STABLE_MS
      ) {
        websocketReconnectDelayMs = min<uint32_t>(
            websocketReconnectDelayMs * 2,
            WS_RECONNECT_MAX_INTERVAL_MS
        );
      } else {
        websocketReconnectDelayMs = WS_RECONNECT_INTERVAL_MS;
      }
      webSocket.setReconnectInterval(websocketReconnectDelayMs);
      websocketConnected = false;
      if (websocketDisconnectedAt == 0) {
        websocketDisconnectedAt = millis();
      }
      cancelAsyncTransceive();
      autonomousDiscoveryEnabled = false;
      discoveryTargetPending = false;
      discoveryWaitForRemoval = false;
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
  const uint32_t now = millis();
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiWasConnected) {
      wifiWasConnected = true;
      wifiConnectedAt = now;
    } else if (
        wifiReconnectDelayMs != WIFI_RETRY_INTERVAL_MS &&
        now - wifiConnectedAt >= WIFI_STABLE_RESET_MS
    ) {
      wifiReconnectDelayMs = WIFI_RETRY_INTERVAL_MS;
    }
    return;
  }

  if (wifiWasConnected) {
    wifiWasConnected = false;
    wifiDisconnectCount++;
    nextWifiAttemptAt = 0;
  }
  if (
      nextWifiAttemptAt != 0 &&
      static_cast<int32_t>(now - nextWifiAttemptAt) < 0
  ) {
    return;
  }

  if (!wifiStarted) {
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    wifiStarted = true;
  } else {
    // Do not call WiFi.disconnect() here: repeatedly tearing down a slow
    // association can keep a marginal reader offline indefinitely.
    WiFi.reconnect();
  }
  nextWifiAttemptAt = now + wifiReconnectDelayMs;
  wifiReconnectDelayMs = min<uint32_t>(
      wifiReconnectDelayMs * 2,
      WIFI_RETRY_MAX_INTERVAL_MS
  );
}

static void recoverStaleWifiPathIfNeeded() {
  if (
      WiFi.status() != WL_CONNECTED ||
      websocketConnected ||
      websocketDisconnectedAt == 0 ||
      millis() - websocketDisconnectedAt < WS_WIFI_RECOVERY_MS
  ) {
    return;
  }
  const uint32_t now = millis();
  if (
      lastWifiPathRecoveryAt != 0 &&
      now - lastWifiPathRecoveryAt < WS_WIFI_RECOVERY_MS
  ) {
    return;
  }

  // Some APs retain an ESP8266 association after its IP data path has become
  // unusable. A single deliberate re-association after a full minute offline
  // forces a new scan/BSSID choice without repeatedly aborting association.
  lastWifiPathRecoveryAt = now;
  wifiDisconnectCount++;
  wifiWasConnected = false;
  wifiReconnectDelayMs = WIFI_RETRY_INTERVAL_MS;
  nextWifiAttemptAt = now + WIFI_RETRY_INTERVAL_MS;
  WiFi.disconnect(false);
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
    cancelAsyncTransceive();
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
    cancelAsyncTransceive();
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

  // A normal 304 check must not interrupt reader service. The updater's
  // onStart callback disconnects only when a real image will be installed.
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

static void setNetworkLed(bool on) {
  // The NodeMCU built-in LED is active low.
  digitalWrite(NETWORK_LED_PIN, on ? LOW : HIGH);
}

static void updateNetworkLed() {
  const uint32_t now = millis();
  if (otaInProgress) {
    // Very rapid blink while flash is being written.
    setNetworkLed((now / 80) % 2);
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    // Fast blink: associating with Wi-Fi.
    setNetworkLed((now / 200) % 2);
    return;
  }
  if (!websocketConnected) {
    // Slow blink: Wi-Fi is up, controller connection is not.
    setNetworkLed((now / 600) % 2);
    return;
  }
  if (!autonomousDiscoveryEnabled) {
    // Double pulse: controller is connected but PN532 is initializing or
    // waiting for its bounded recovery cycle.
    const uint32_t phase = now % 2000;
    setNetworkLed(phase < 120 || (phase >= 240 && phase < 360));
    return;
  }
  // Solid: Wi-Fi, controller WebSocket, and PN532 discovery are ready.
  setNetworkLed(true);
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
  // GPIO2/D4 is a boot strap pin. Keep it high while changing it to output;
  // the on-board LED is therefore off until firmware is fully running.
  digitalWrite(NETWORK_LED_PIN, HIGH);
  pinMode(NETWORK_LED_PIN, OUTPUT);
  setNetworkLed(false);
  pinMode(STATUS_LED_PIN, OUTPUT);
  setStatusLed(false);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  // Set the inactive level before changing the pin to output, avoiding an
  // unintended reset pulse during GPIO initialization.
  digitalWrite(PN532_RESET_PIN, HIGH);
  pinMode(PN532_RESET_PIN, OUTPUT);
  buttonRawPressed = digitalRead(BUTTON_PIN) == LOW;
  buttonStablePressed = buttonRawPressed;

  Serial.setRxBufferSize(PN532_RING_SIZE);
  Serial.begin(PN532_BAUD_RATE, SERIAL_8N1);
  Serial.setTimeout(2);
  hardwareResetPn532();

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
    updateNetworkLed();
    yield();
    return;
  }
  webSocket.loop();
  recoverStaleWifiPathIfNeeded();
  if (
      websocketConnected &&
      websocketReconnectDelayMs != WS_RECONNECT_INTERVAL_MS &&
      millis() - websocketConnectedAt >= WS_RECONNECT_STABLE_MS
  ) {
    websocketReconnectDelayMs = WS_RECONNECT_INTERVAL_MS;
    webSocket.setReconnectInterval(websocketReconnectDelayMs);
  }
  updateButton();
  drainPn532Uart();
  updateAsyncTransceive();
  runAutonomousDiscovery();
  updateStatusLed();
  updateNetworkLed();
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
