from __future__ import annotations

import unittest

from homekey_bridge.protocol import (
    ErrorCode,
    Message,
    MessageType,
    ReaderRuntimeState,
    normalize_reader_id,
)


class ProtocolTests(unittest.TestCase):
    def test_binary_message_round_trip(self) -> None:
        message = Message(
            MessageType.EXECUTE,
            request_id=0x10203040,
            timeout_ms=750,
            payload=bytes.fromhex("0000ff02fed4022a00"),
        )
        self.assertEqual(Message.decode(message.encode()), message)

    def test_rejects_mismatched_payload_length(self) -> None:
        encoded = Message(MessageType.RESET, request_id=1).encode()
        with self.assertRaisesRegex(ValueError, "payload length"):
            Message.decode(encoded + b"\x00")

    def test_access_result_round_trip(self) -> None:
        message = Message(
            type=MessageType.ACCESS_RESULT,
            request_id=44,
            timeout_ms=500,
            payload=b"\x01\x00\x00\x13\x88",
        )
        self.assertEqual(Message.decode(message.encode()), message)

    def test_discover_round_trip(self) -> None:
        message = Message(
            type=MessageType.DISCOVER,
            request_id=45,
            timeout_ms=1500,
            payload=bytes.fromhex("6a020000000000000000"),
        )
        self.assertEqual(Message.decode(message.encode()), message)

    def test_autonomous_discovery_messages_round_trip(self) -> None:
        start = Message(
            type=MessageType.START_DISCOVERY,
            request_id=46,
            timeout_ms=500,
            payload=bytes.fromhex("6a020000000000000000"),
        )
        target = Message(
            type=MessageType.TARGET_EVENT,
            request_id=0x90000000,
            payload=bytes.fromhex("0101014400200404A1B2C3"),
        )
        resume = Message(
            type=MessageType.RESUME_DISCOVERY,
            request_id=47,
        )
        for message in (start, target, resume):
            self.assertEqual(Message.decode(message.encode()), message)

    def test_transceive_round_trip(self) -> None:
        message = Message(
            type=MessageType.TRANSCEIVE,
            request_id=48,
            timeout_ms=30,
            payload=bytes.fromhex("E080"),
        )
        self.assertEqual(Message.decode(message.encode()), message)

    def test_button_event_and_result_round_trip(self) -> None:
        event = Message(
            type=MessageType.BUTTON_EVENT,
            request_id=0x80000001,
        )
        result = Message(
            type=MessageType.BUTTON_RESULT,
            request_id=event.request_id,
            payload=b"\x01",
        )
        self.assertEqual(Message.decode(event.encode()), event)
        self.assertEqual(Message.decode(result.encode()), result)

    def test_firmware_update_check_round_trip(self) -> None:
        message = Message(
            type=MessageType.FIRMWARE_UPDATE_CHECK,
            request_id=77,
        )
        self.assertEqual(Message.decode(message.encode()), message)

    def test_reader_status_round_trip(self) -> None:
        message = Message(
            type=MessageType.READER_STATUS,
            request_id=0xA0000000,
            payload=bytes(
                [
                    ReaderRuntimeState.FAILED,
                    ErrorCode.PN532_TIMEOUT,
                    3,
                ]
            ),
        )
        self.assertEqual(Message.decode(message.encode()), message)

    def test_normalizes_mac_reader_id(self) -> None:
        self.assertEqual(
            normalize_reader_id("C8:C9:A3:38:59:AF"),
            "c8c9a33859af",
        )
        self.assertEqual(
            normalize_reader_id("c8-c9-a3-38-59-af"),
            "c8c9a33859af",
        )

    def test_rejects_invalid_reader_id(self) -> None:
        with self.assertRaises(ValueError):
            normalize_reader_id("front-door")


if __name__ == "__main__":
    unittest.main()
