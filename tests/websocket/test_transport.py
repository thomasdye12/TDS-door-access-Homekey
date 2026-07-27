from __future__ import annotations

import unittest

from homekey_bridge.protocol import MessageType
from homekey_bridge.transport import WebSocketPn532Transport


ACK = bytes.fromhex("0000ff00ff00")
RESPONSE = bytes.fromhex("0000ff06fad50332010607e800")


class FakeConnection:
    def __init__(self, response: bytes = RESPONSE) -> None:
        self.requests: list[tuple[MessageType, bytes, int]] = []
        self.response = response

    def request(
        self,
        message_type: MessageType,
        *,
        payload: bytes = b"",
        timeout_ms: int = 500,
    ) -> bytes:
        self.requests.append((message_type, payload, timeout_ms))
        if message_type == MessageType.EXECUTE:
            return ACK + self.response
        if message_type == MessageType.TRANSCEIVE:
            return bytes.fromhex("009000")
        return b""


class FakeManager:
    def __init__(self, response: bytes = RESPONSE) -> None:
        self.connection = FakeConnection(response)

    def wait_for(self, _reader_id: str, timeout: float):
        return self.connection


class TransportTests(unittest.TestCase):
    def test_nfcpy_write_then_two_reads_become_execute_and_read(self) -> None:
        manager = FakeManager()
        transport = WebSocketPn532Transport(manager, "c8:c9:a3:38:59:af")
        transport.open(transport.port)
        command = bytes.fromhex("0000ff02fed4022a00")
        transport.write(command)

        self.assertEqual(transport.read(100), bytearray(ACK))
        self.assertEqual(transport.read(100), bytearray(RESPONSE))
        self.assertEqual(
            manager.connection.requests,
            [
                (MessageType.RESET, b"", 500),
                (MessageType.EXECUTE, command, 250),
            ],
        )

    def test_read_without_command_fails(self) -> None:
        transport = WebSocketPn532Transport(
            FakeManager(), "c8c9a33859af"
        )
        with self.assertRaisesRegex(IOError, "without a pending command"):
            transport.read(100)

    def test_iso_dep_transceive_is_one_reader_request(self) -> None:
        manager = FakeManager()
        transport = WebSocketPn532Transport(
            manager, "c8c9a33859af"
        )

        status, response = transport.transceive(
            bytes.fromhex("E080"), 0.03
        )

        self.assertEqual(status, 0)
        self.assertEqual(response, bytearray.fromhex("9000"))
        self.assertEqual(
            manager.connection.requests,
            [
                (
                    MessageType.TRANSCEIVE,
                    bytes.fromhex("E080"),
                    30,
                )
            ],
        )

    def test_extended_diagnostic_response_is_cached_intact(self) -> None:
        diagnostic_response = (
            bytes.fromhex("0000ffffff0101fe")
            + bytes(range(256))
            + bytes.fromhex("0000")
        )
        manager = FakeManager(diagnostic_response)
        transport = WebSocketPn532Transport(manager, "c8c9a33859af")
        transport.open(transport.port)
        transport.write(bytes.fromhex("0000ff02fed4002c00"))

        self.assertEqual(transport.read(100), bytearray(ACK))
        self.assertEqual(
            transport.read(100),
            bytearray(diagnostic_response),
        )


if __name__ == "__main__":
    unittest.main()
