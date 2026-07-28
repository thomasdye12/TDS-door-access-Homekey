from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from homekey_controller.reader_enrollment import enroll_fleet_reader


class AutoEnrollmentTests(unittest.TestCase):
    @staticmethod
    def token(secret: str, reader_id: str) -> str:
        return hmac.new(
            secret.encode("utf-8"),
            reader_id.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def test_valid_fleet_reader_is_persisted_and_started(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readers.json"
            path.write_text(
                json.dumps(
                    {
                        "fleet_token": "fleet-secret",
                        "auto_enroll": True,
                        "max_readers": 10,
                        "custom_setting": "preserve-me",
                        "readers": {},
                    }
                ),
                encoding="utf-8",
            )
            reader_id = "a848fac0c39a"

            record = enroll_fleet_reader(
                path,
                "shared-door-controller",
                reader_id,
                self.token("fleet-secret", reader_id),
            )

            self.assertIsNotNone(record)
            self.assertEqual(record.door_id, "shared-door-controller")
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["custom_setting"],
                "preserve-me",
            )
            self.assertEqual(
                persisted["readers"][reader_id],
                {
                    "door_id": "shared-door-controller",
                    "enabled": True,
                },
            )

    def test_invalid_token_does_not_change_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readers.json"
            original = {
                "fleet_token": "fleet-secret",
                "readers": {},
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            record = enroll_fleet_reader(
                path,
                "shared-door-controller",
                "a848fac0c39a",
                "invalid",
            )

            self.assertIsNone(record)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                original,
            )

    def test_reader_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readers.json"
            path.write_text(
                json.dumps(
                    {
                        "fleet_token": "fleet-secret",
                        "max_readers": 1,
                        "readers": {
                            "c8c9a33859af": {
                                "door_id": "shared-door-controller",
                                "enabled": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            reader_id = "a848fac0c39a"

            record = enroll_fleet_reader(
                path,
                "shared-door-controller",
                reader_id,
                self.token("fleet-secret", reader_id),
            )

            self.assertIsNone(record)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(reader_id, persisted["readers"])


if __name__ == "__main__":
    unittest.main()
