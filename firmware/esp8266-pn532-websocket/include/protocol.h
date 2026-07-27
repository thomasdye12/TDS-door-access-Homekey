#pragma once

#include <Arduino.h>
#include "bridge_config.h"

constexpr uint8_t PROTOCOL_MAGIC_0 = 'H';
constexpr uint8_t PROTOCOL_MAGIC_1 = 'K';
constexpr uint8_t PROTOCOL_VERSION = 1;
constexpr size_t PROTOCOL_HEADER_SIZE = 12;

enum class MessageType : uint8_t {
  EXECUTE = 0x10,
  READ_FRAME = 0x11,
  RESET = 0x12,
  ACCESS_RESULT = 0x13,
  BUTTON_EVENT = 0x20,
  BUTTON_RESULT = 0x21,
  FIRMWARE_UPDATE_CHECK = 0x22,
  RESPONSE = 0x80,
  ERROR_RESPONSE = 0x7F,
};

enum class ErrorCode : uint8_t {
  BAD_MESSAGE = 1,
  BAD_VERSION = 2,
  UNSUPPORTED_TYPE = 3,
  PN532_TIMEOUT = 4,
  PN532_BAD_ACK = 5,
  PN532_FRAME_TOO_LARGE = 6,
  PN532_BUSY = 7,
  PN532_BAD_FRAME = 8,
};

struct ProtocolMessage {
  MessageType type;
  uint32_t requestId;
  uint16_t timeoutMs;
  uint16_t payloadLength;
  const uint8_t* payload;
};

inline uint16_t readU16(const uint8_t* data) {
  return (static_cast<uint16_t>(data[0]) << 8) | data[1];
}

inline uint32_t readU32(const uint8_t* data) {
  return (static_cast<uint32_t>(data[0]) << 24) |
         (static_cast<uint32_t>(data[1]) << 16) |
         (static_cast<uint32_t>(data[2]) << 8) |
         data[3];
}

inline void writeU16(uint8_t* data, uint16_t value) {
  data[0] = static_cast<uint8_t>(value >> 8);
  data[1] = static_cast<uint8_t>(value);
}

inline void writeU32(uint8_t* data, uint32_t value) {
  data[0] = static_cast<uint8_t>(value >> 24);
  data[1] = static_cast<uint8_t>(value >> 16);
  data[2] = static_cast<uint8_t>(value >> 8);
  data[3] = static_cast<uint8_t>(value);
}

inline bool decodeMessage(
    const uint8_t* data,
    size_t length,
    ProtocolMessage& message
) {
  if (length < PROTOCOL_HEADER_SIZE ||
      data[0] != PROTOCOL_MAGIC_0 ||
      data[1] != PROTOCOL_MAGIC_1 ||
      data[2] != PROTOCOL_VERSION) {
    return false;
  }

  const uint16_t payloadLength = readU16(data + 10);
  if (payloadLength > MAX_PROTOCOL_PAYLOAD ||
      length != PROTOCOL_HEADER_SIZE + payloadLength) {
    return false;
  }

  message.type = static_cast<MessageType>(data[3]);
  message.requestId = readU32(data + 4);
  message.timeoutMs = readU16(data + 8);
  message.payloadLength = payloadLength;
  message.payload = data + PROTOCOL_HEADER_SIZE;
  return true;
}
