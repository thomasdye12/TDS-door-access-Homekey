from __future__ import annotations

import unittest

from homekey_bridge.protocol import Message, MessageType, normalize_reader_id


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
