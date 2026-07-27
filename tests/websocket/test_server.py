from __future__ import annotations

import json
import hashlib
import hmac
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from websockets.sync.client import connect

from homekey_bridge.protocol import Message, MessageType
from homekey_bridge.server import (
    ReaderManager,
    ReaderRecord,
    ReaderWebSocketServer,
    load_registry,
)


READER_ID = "c8c9a33859af"
TOKEN = "test-token"
ACK = bytes.fromhex("0000ff00ff00")
PN532_RESPONSE = bytes.fromhex("0000ff06fad50332010607e800")


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class ServerTests(unittest.TestCase):
    def test_registry_derives_reader_token_from_fleet_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readers.json"
            path.write_text(
                json.dumps(
                    {
                        "fleet_token": "fleet-secret",
                        "readers": {
                            "C8:C9:A3:38:59:AF": {
                                "door_id": "front-door",
                                "enabled": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            registry = load_registry(path)

        expected = hmac.new(
            b"fleet-secret",
            READER_ID.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(registry[READER_ID].token, expected)

    def test_registry_preserves_explicit_token_for_fleet_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readers.json"
            path.write_text(
                json.dumps(
                    {
                        "fleet_token": "fleet-secret",
                        "readers": {
                            READER_ID: {
                                "door_id": "front-door",
                                "token": "reader-specific",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            registry = load_registry(path)

        expected = hmac.new(
            b"fleet-secret",
            READER_ID.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(registry[READER_ID].token, expected)
        self.assertEqual(
            registry[READER_ID].legacy_token, "reader-specific"
        )

    def test_authenticated_reader_and_correlated_binary_requests(self) -> None:
        record = ReaderRecord(READER_ID, "front-door", TOKEN, True)
        manager = ReaderManager({READER_ID: record})
        port = unused_port()
        server = ReaderWebSocketServer(manager, "127.0.0.1", port)
        server.start()
        reader_finished = threading.Event()

        def fake_reader() -> None:
            with connect(f"ws://127.0.0.1:{port}/readers") as websocket:
                websocket.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "protocol": 1,
                            "reader_id": "C8:C9:A3:38:59:AF",
                            "token": TOKEN,
                            "firmware": "test",
                        }
                    )
                )
                for raw_request in websocket:
                    request = Message.decode(raw_request)
                    payload = ACK + PN532_RESPONSE
                    websocket.send(
                        Message(
                            MessageType.RESPONSE,
                            request.request_id,
                            payload=payload,
                        ).encode()
                    )
                    break
            reader_finished.set()

        reader = threading.Thread(target=fake_reader, daemon=True)
        reader.start()
        try:
            connection = manager.wait_for(READER_ID, timeout=2)
            self.assertEqual(connection.record.door_id, "front-door")
            self.assertEqual(
                connection.request(
                    MessageType.EXECUTE,
                    payload=b"command",
                    timeout_ms=250,
                ),
                ACK + PN532_RESPONSE,
            )
            self.assertTrue(reader_finished.wait(2))
        finally:
            server.stop()
            reader.join(timeout=2)

    def test_button_event_is_handled_and_acknowledged(self) -> None:
        record = ReaderRecord(READER_ID, "front-door", TOKEN, True)
        manager = ReaderManager({READER_ID: record})
        handled = threading.Event()

        def button_handler(received_record):
            self.assertEqual(received_record.reader_id, READER_ID)
            handled.set()
            return True

        port = unused_port()
        server = ReaderWebSocketServer(
            manager,
            "127.0.0.1",
            port,
            button_handler=button_handler,
        )
        server.start()
        try:
            with connect(
                f"ws://127.0.0.1:{port}/readers"
            ) as websocket:
                websocket.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "protocol": 1,
                            "reader_id": READER_ID,
                            "token": TOKEN,
                            "firmware": "test",
                        }
                    )
                )
                websocket.send(
                    Message(
                        MessageType.BUTTON_EVENT,
                        request_id=0x80000001,
                    ).encode()
                )
                response = Message.decode(websocket.recv(timeout=2))
            self.assertTrue(handled.wait(2))
            self.assertEqual(response.type, MessageType.BUTTON_RESULT)
            self.assertEqual(response.request_id, 0x80000001)
            self.assertEqual(response.payload, b"\x01")
        finally:
            server.stop()

    def test_target_event_is_delivered_to_reader_worker(self) -> None:
        record = ReaderRecord(READER_ID, "front-door", TOKEN, True)
        manager = ReaderManager({READER_ID: record})
        port = unused_port()
        server = ReaderWebSocketServer(manager, "127.0.0.1", port)
        server.start()
        target = bytes.fromhex("0101014400200404A1B2C3")
        try:
            with connect(
                f"ws://127.0.0.1:{port}/readers"
            ) as websocket:
                websocket.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "protocol": 1,
                            "reader_id": READER_ID,
                            "token": TOKEN,
                            "firmware": "3.0.0",
                        }
                    )
                )
                connection = manager.wait_for(READER_ID, timeout=2)
                websocket.send(
                    Message(
                        MessageType.TARGET_EVENT,
                        request_id=0x90000000,
                        payload=target,
                    ).encode()
                )
                self.assertEqual(
                    connection.wait_for_target(timeout=2),
                    target,
                )
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
