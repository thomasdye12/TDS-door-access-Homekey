from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from homekey_controller.store import SQLiteKeyStore


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = SQLiteKeyStore(root / "db.sqlite3", root / "db.key")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_import_mapping_and_audit(self) -> None:
        root = Path(self.temporary.name)
        endpoint_public_key = "04" + ("11" * 64)
        document = {
            "reader_private_key": "22" * 32,
            "reader_identifier": "33" * 8,
            "issuers": {
                "ignored": {
                    "public_key": "44" * 32,
                    "endpoints": {
                        "ignored": {
                            "last_used_at": 1,
                            "counter": 2,
                            "key_type": 2,
                            "public_key": endpoint_public_key,
                            "persistent_key": "55" * 32,
                            "enrollments": {
                                "hap": {"at": 1, "payload": "payload"},
                                "attestation": None,
                            },
                        }
                    },
                }
            },
        }
        legacy = root / "homekey.json"
        legacy.write_text(json.dumps(document), encoding="utf-8")

        result = self.store.import_legacy_document(legacy)
        self.assertEqual(result, {"issuers": 1, "endpoints": 1})
        endpoint = self.store.get_all_endpoints()[0]
        self.store.map_endpoint(endpoint.id.hex(), "user-123")
        self.assertEqual(
            self.store.resolve_user(endpoint.id.hex()), "user-123"
        )
        self.store.map_credential("rfid_uid", "04A1B2C3", "card-user")
        self.assertEqual(
            self.store.resolve_credential_user("rfid_uid", "04a1b2c3"),
            "card-user",
        )

        event = self.store.new_event(
            event_id="event-1",
            controller_id="controller",
            door_id="door",
            reader_id="001122334455",
            credential_type="apple_home_key",
            credential_id=endpoint.id.hex(),
            endpoint_id=endpoint.id.hex(),
            user_id="user-123",
            granted=True,
            reason="allowed",
            api_status=200,
            duration_ms=12.5,
        )
        self.store.record_event(event)
        self.assertEqual(self.store.list_events()[0]["event_id"], "event-1")
        self.assertEqual(
            self.store.list_events()[0]["credential_type"],
            "apple_home_key",
        )

        database_bytes = (root / "db.sqlite3").read_bytes()
        self.assertNotIn(bytes.fromhex("22" * 32), database_bytes)
        self.assertNotIn(bytes.fromhex("55" * 32), database_bytes)

    def test_refuses_to_replace_existing_keys_without_force(self) -> None:
        root = Path(self.temporary.name)
        legacy = root / "homekey.json"
        legacy.write_text(
            json.dumps(
                {
                    "reader_private_key": "22" * 32,
                    "reader_identifier": "33" * 8,
                    "issuers": {},
                }
            ),
            encoding="utf-8",
        )
        self.store.import_legacy_document(legacy)
        with self.assertRaisesRegex(RuntimeError, "already configured"):
            self.store.import_legacy_document(legacy)

    def test_migrates_existing_home_key_audit_rows(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        try:
            root = Path(temporary.name)
            database = root / "legacy.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE access_events (
                        event_id TEXT PRIMARY KEY,
                        occurred_at INTEGER NOT NULL,
                        controller_id TEXT NOT NULL,
                        door_id TEXT NOT NULL,
                        reader_id TEXT NOT NULL,
                        endpoint_id TEXT NOT NULL,
                        user_id TEXT,
                        granted INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        api_status INTEGER,
                        duration_ms REAL NOT NULL
                    );
                    INSERT INTO access_events VALUES(
                        'old-event', 1, 'controller', 'door',
                        '001122334455', 'aabbccddeeff', NULL,
                        1, 'allowed', 200, 10.0
                    );
                    """
                )

            migrated = SQLiteKeyStore(database, root / "key")
            event = migrated.list_events()[0]
            self.assertEqual(event["credential_type"], "apple_home_key")
            self.assertEqual(event["credential_id"], "aabbccddeeff")
            self.assertEqual(event["endpoint_id"], "aabbccddeeff")
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
