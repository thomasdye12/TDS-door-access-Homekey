from __future__ import annotations

import base64
import hashlib
import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from homekey_bridge.server import ReaderManager, ReaderRecord
from homekey_controller.config import FirmwareServerConfig
from homekey_controller.firmware_server import FirmwareHttpServer


READER_ID = "c8c9a33859af"
READER_TOKEN = "reader-token"


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class FirmwareServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        binary = b"\xE9test-esp8266-firmware"
        binary_path = directory / "firmware-2.4.0.bin"
        binary_path.write_bytes(binary)
        self.binary = binary
        self.manifest_path = directory / "manifest.json"
        self.manifest_path.write_text(
            json.dumps(
                {
                    "version": "2.4.0",
                    "binary": binary_path.name,
                    "size": len(binary),
                    "md5": hashlib.md5(binary).hexdigest(),
                    "sha256": hashlib.sha256(binary).hexdigest(),
                    "targets": ["*"],
                }
            ),
            encoding="utf-8",
        )
        self.port = unused_port()
        manager = ReaderManager(
            {
                READER_ID: ReaderRecord(
                    READER_ID,
                    "front-door",
                    READER_TOKEN,
                    True,
                )
            }
        )
        self.server = FirmwareHttpServer(
            FirmwareServerConfig(
                enabled=True,
                host="127.0.0.1",
                port=self.port,
                manifest_path=self.manifest_path,
                admin_token="admin-token",
            ),
            SimpleNamespace(
                controller_id="controller",
                door_id="door",
            ),
            manager,
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.temporary.cleanup()

    def _reader_authorization(self) -> str:
        encoded = base64.b64encode(
            f"{READER_ID}:{READER_TOKEN}".encode()
        ).decode()
        return f"Basic {encoded}"

    def test_health_and_authenticated_binary_download(self) -> None:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/health"
        ) as response:
            health = json.load(response)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["firmware_version"], "2.4.0")

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/firmware/latest",
            headers={
                "Authorization": self._reader_authorization(),
                "x-ESP8266-version": "2.3.0",
            },
        )
        with urllib.request.urlopen(request) as response:
            body = response.read()
            md5 = response.headers["x-MD5"]
        self.assertEqual(body, self.binary)
        self.assertEqual(md5, hashlib.md5(self.binary).hexdigest())

    def test_current_version_receives_not_modified(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/firmware/latest",
            headers={
                "Authorization": self._reader_authorization(),
                "x-ESP8266-version": "2.4.0",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 304)

    def test_admin_can_target_rollout(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/firmware/rollout",
            data=json.dumps({"targets": [READER_ID]}).encode(),
            headers={
                "Authorization": "Bearer admin-token",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["targets"], [READER_ID])
        manifest = json.loads(self.manifest_path.read_text())
        self.assertEqual(manifest["targets"], [READER_ID])


if __name__ == "__main__":
    unittest.main()
