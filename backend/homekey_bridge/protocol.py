from __future__ import annotations

import dataclasses
import enum
import struct


MAGIC = b"HK"
VERSION = 1
HEADER = struct.Struct(">2sBBIHH")
MAX_PAYLOAD = 1100


class MessageType(enum.IntEnum):
    EXECUTE = 0x10
    READ_FRAME = 0x11
    RESET = 0x12
    ACCESS_RESULT = 0x13
    BUTTON_EVENT = 0x20
    BUTTON_RESULT = 0x21
    FIRMWARE_UPDATE_CHECK = 0x22
    RESPONSE = 0x80
    ERROR_RESPONSE = 0x7F


class ErrorCode(enum.IntEnum):
    BAD_MESSAGE = 1
    BAD_VERSION = 2
    UNSUPPORTED_TYPE = 3
    PN532_TIMEOUT = 4
    PN532_BAD_ACK = 5
    PN532_FRAME_TOO_LARGE = 6
    PN532_BUSY = 7
    PN532_BAD_FRAME = 8


@dataclasses.dataclass(frozen=True)
class Message:
    type: MessageType
    request_id: int
    timeout_ms: int = 0
    payload: bytes = b""

    def encode(self) -> bytes:
        if not 0 <= self.request_id <= 0xFFFFFFFF:
            raise ValueError("request_id must fit uint32")
        if not 0 <= self.timeout_ms <= 0xFFFF:
            raise ValueError("timeout_ms must fit uint16")
        if len(self.payload) > MAX_PAYLOAD:
            raise ValueError("payload is too large")
        return HEADER.pack(
            MAGIC,
            VERSION,
            int(self.type),
            self.request_id,
            self.timeout_ms,
            len(self.payload),
        ) + self.payload

    @classmethod
    def decode(cls, data: bytes) -> "Message":
        if len(data) < HEADER.size:
            raise ValueError("message is shorter than the protocol header")
        magic, version, message_type, request_id, timeout_ms, payload_length = (
            HEADER.unpack_from(data)
        )
        if magic != MAGIC:
            raise ValueError("invalid protocol magic")
        if version != VERSION:
            raise ValueError(f"unsupported protocol version {version}")
        if payload_length > MAX_PAYLOAD:
            raise ValueError("payload is too large")
        if len(data) != HEADER.size + payload_length:
            raise ValueError("payload length does not match message")
        return cls(
            type=MessageType(message_type),
            request_id=request_id,
            timeout_ms=timeout_ms,
            payload=data[HEADER.size:],
        )


def normalize_reader_id(value: str) -> str:
    normalized = value.lower().replace(":", "").replace("-", "")
    if len(normalized) != 12:
        raise ValueError("reader ID must be a 6-byte MAC address")
    try:
        bytes.fromhex(normalized)
    except ValueError as error:
        raise ValueError("reader ID must contain hexadecimal MAC bytes") from error
    return normalized
