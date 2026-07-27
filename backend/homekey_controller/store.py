from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .vendor import activate_vendor

activate_vendor()

from entity import Endpoint, Issuer  # noqa: E402


ZERO_READER_KEY = bytes(32)
ZERO_READER_IDENTIFIER = bytes(8)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccessEvent:
    event_id: str
    occurred_at: int
    controller_id: str
    door_id: str
    reader_id: str
    credential_type: str
    credential_id: str
    endpoint_id: str | None
    user_id: str | None
    granted: bool
    reason: str
    api_status: int | None
    duration_ms: float


class SQLiteKeyStore:
    """Encrypted shared Home Key state plus mappings and audit events."""

    def __init__(self, path: Path, encryption_key_file: Path) -> None:
        self.path = path.expanduser().resolve()
        self.encryption_key_file = encryption_key_file.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._load_or_create_key())
        self._lock = threading.RLock()
        self._reader_private_key = ZERO_READER_KEY
        self._reader_identifier = ZERO_READER_IDENTIFIER
        self._issuers: list[Issuer] = []
        self._initialize_database()
        self._load_state()

    def _load_or_create_key(self) -> bytes:
        try:
            key = self.encryption_key_file.read_bytes().strip()
        except FileNotFoundError:
            self.encryption_key_file.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(self.encryption_key_file, flags, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(key + b"\n")
        self._make_private(self.encryption_key_file)
        Fernet(key)
        return key

    @staticmethod
    def _make_private(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except PermissionError:
            log.warning(
                "Filesystem does not support chmod for %s; enforce access "
                "using the server-side mount/share permissions",
                path,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS key_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    encrypted_document BLOB NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS endpoint_users (
                    endpoint_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS credential_users (
                    credential_type TEXT NOT NULL,
                    credential_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(credential_type, credential_id)
                );

                CREATE TABLE IF NOT EXISTS access_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at INTEGER NOT NULL,
                    controller_id TEXT NOT NULL,
                    door_id TEXT NOT NULL,
                    reader_id TEXT NOT NULL,
                    credential_type TEXT NOT NULL,
                    credential_id TEXT NOT NULL,
                    endpoint_id TEXT,
                    user_id TEXT,
                    granted INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    api_status INTEGER,
                    duration_ms REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS access_events_occurred_at
                    ON access_events(occurred_at);
                CREATE INDEX IF NOT EXISTS access_events_endpoint_id
                    ON access_events(endpoint_id);
                """
            )
            self._migrate_access_events(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS access_events_credential
                ON access_events(credential_type, credential_id)
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO credential_users(
                    credential_type, credential_id, user_id, enabled, updated_at
                )
                SELECT 'apple_home_key', endpoint_id, user_id, enabled, updated_at
                FROM endpoint_users
                """
            )
        self._make_private(self.path)

    def _migrate_access_events(
        self, connection: sqlite3.Connection
    ) -> None:
        columns = connection.execute(
            "PRAGMA table_info(access_events)"
        ).fetchall()
        names = {row["name"] for row in columns}
        endpoint_not_null = next(
            (
                bool(row["notnull"])
                for row in columns
                if row["name"] == "endpoint_id"
            ),
            False,
        )
        if (
            {"credential_type", "credential_id"}.issubset(names)
            and not endpoint_not_null
        ):
            return
        connection.executescript(
            """
            ALTER TABLE access_events RENAME TO access_events_legacy;
            DROP INDEX IF EXISTS access_events_occurred_at;
            DROP INDEX IF EXISTS access_events_endpoint_id;

            CREATE TABLE access_events (
                event_id TEXT PRIMARY KEY,
                occurred_at INTEGER NOT NULL,
                controller_id TEXT NOT NULL,
                door_id TEXT NOT NULL,
                reader_id TEXT NOT NULL,
                credential_type TEXT NOT NULL,
                credential_id TEXT NOT NULL,
                endpoint_id TEXT,
                user_id TEXT,
                granted INTEGER NOT NULL,
                reason TEXT NOT NULL,
                api_status INTEGER,
                duration_ms REAL NOT NULL
            );

            INSERT INTO access_events(
                event_id, occurred_at, controller_id, door_id, reader_id,
                credential_type, credential_id, endpoint_id, user_id, granted,
                reason, api_status, duration_ms
            )
            SELECT
                event_id, occurred_at, controller_id, door_id, reader_id,
                'apple_home_key', endpoint_id, endpoint_id, user_id, granted,
                reason, api_status, duration_ms
            FROM access_events_legacy;

            DROP TABLE access_events_legacy;
            CREATE INDEX access_events_occurred_at
                ON access_events(occurred_at);
            CREATE INDEX access_events_endpoint_id
                ON access_events(endpoint_id);
            CREATE INDEX access_events_credential
                ON access_events(credential_type, credential_id);
            """
        )

    def _state_document(self) -> dict[str, Any]:
        return {
            "reader_private_key": self._reader_private_key.hex(),
            "reader_identifier": self._reader_identifier.hex(),
            "issuers": {
                issuer.id.hex(): issuer.to_dict() for issuer in self._issuers
            },
        }

    def _load_document(self, document: dict[str, Any]) -> None:
        self._reader_private_key = bytes.fromhex(
            document.get("reader_private_key", ZERO_READER_KEY.hex())
        )
        self._reader_identifier = bytes.fromhex(
            document.get(
                "reader_identifier", ZERO_READER_IDENTIFIER.hex()
            )
        )
        self._issuers = [
            Issuer.from_dict(raw)
            for raw in document.get("issuers", {}).values()
        ]

    def _load_state(self) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT encrypted_document FROM key_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                self._persist_locked(connection)
                return
            try:
                plaintext = self._fernet.decrypt(row["encrypted_document"])
            except InvalidToken as error:
                raise RuntimeError(
                    "controller database cannot be decrypted with its key file"
                ) from error
            self._load_document(json.loads(plaintext))

    def _persist_locked(
        self, connection: sqlite3.Connection | None = None
    ) -> None:
        encrypted = self._fernet.encrypt(
            json.dumps(
                self._state_document(),
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            connection.execute(
                """
                INSERT INTO key_state(singleton, encrypted_document, updated_at)
                VALUES(1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    encrypted_document = excluded.encrypted_document,
                    updated_at = excluded.updated_at
                """,
                (encrypted, int(time.time())),
            )
            if owns_connection:
                connection.commit()
        finally:
            if owns_connection:
                connection.close()

    def import_legacy_document(
        self, source_path: Path, *, force: bool = False
    ) -> dict[str, int]:
        with source_path.expanduser().resolve().open(
            "r", encoding="utf-8"
        ) as source:
            document = json.load(source)
        candidate_issuers = [
            Issuer.from_dict(raw)
            for raw in document.get("issuers", {}).values()
        ]
        private_key = bytes.fromhex(document["reader_private_key"])
        reader_identifier = bytes.fromhex(document["reader_identifier"])
        if len(private_key) != 32 or len(reader_identifier) != 8:
            raise ValueError("legacy Home Key state has invalid key lengths")

        with self._lock:
            configured = (
                self._reader_private_key != ZERO_READER_KEY
                or bool(self._issuers)
            )
            if configured and not force:
                raise RuntimeError(
                    "controller key store is already configured; refusing import"
                )
            self._reader_private_key = private_key
            self._reader_identifier = reader_identifier
            self._issuers = candidate_issuers
            self._persist_locked()
        return {
            "issuers": len(candidate_issuers),
            "endpoints": sum(
                len(issuer.endpoints) for issuer in candidate_issuers
            ),
        }

    def is_configured(self) -> bool:
        with self._lock:
            return self._reader_private_key != ZERO_READER_KEY

    def get_reader_private_key(self) -> bytes:
        with self._lock:
            return bytes(self._reader_private_key)

    def set_reader_private_key(self, value: bytes) -> None:
        if len(value) != 32:
            raise ValueError("reader private key must be 32 bytes")
        with self._lock:
            self._reader_private_key = bytes(value)
            self._persist_locked()

    def get_reader_identifier(self) -> bytes:
        with self._lock:
            return bytes(self._reader_identifier)

    def set_reader_identifier(self, value: bytes) -> None:
        if len(value) != 8:
            raise ValueError("reader identifier must be 8 bytes")
        with self._lock:
            self._reader_identifier = bytes(value)
            self._persist_locked()

    def get_reader_group_identifier(self) -> bytes:
        return hashlib.sha256(
            b"key-identifier" + self.get_reader_private_key()
        ).digest()[:8]

    def get_all_issuers(self) -> list[Issuer]:
        with self._lock:
            return copy.deepcopy(self._issuers)

    def get_all_endpoints(self) -> list[Endpoint]:
        with self._lock:
            return copy.deepcopy(
                [
                    endpoint
                    for issuer in self._issuers
                    for endpoint in issuer.endpoints
                ]
            )

    def get_endpoint_by_public_key(
        self, public_key: bytes
    ) -> Endpoint | None:
        return next(
            (
                endpoint
                for endpoint in self.get_all_endpoints()
                if endpoint.public_key == public_key
            ),
            None,
        )

    def get_endpoint_by_id(self, endpoint_id: bytes) -> Endpoint | None:
        return next(
            (
                endpoint
                for endpoint in self.get_all_endpoints()
                if endpoint.id == endpoint_id
            ),
            None,
        )

    def get_issuer_by_id(self, issuer_id: bytes) -> Issuer | None:
        return next(
            (
                issuer
                for issuer in self.get_all_issuers()
                if issuer.id == issuer_id
            ),
            None,
        )

    def get_issuer_by_public_key(
        self, public_key: bytes
    ) -> Issuer | None:
        return next(
            (
                issuer
                for issuer in self.get_all_issuers()
                if issuer.public_key == public_key
            ),
            None,
        )

    def remove_issuer(self, issuer: Issuer) -> None:
        with self._lock:
            self._issuers = [
                item for item in self._issuers if item.id != issuer.id
            ]
            self._persist_locked()

    def upsert_issuer(self, issuer: Issuer) -> None:
        with self._lock:
            replacement = copy.deepcopy(issuer)
            for index, current in enumerate(self._issuers):
                if current.id == replacement.id:
                    self._issuers[index] = replacement
                    break
            else:
                self._issuers.append(replacement)
            self._persist_locked()

    def upsert_endpoint(self, issuer_id: bytes, endpoint: Endpoint) -> None:
        with self._lock:
            issuer = next(
                (
                    current
                    for current in self._issuers
                    if current.id == issuer_id
                ),
                None,
            )
            if issuer is None:
                raise KeyError(f"issuer {issuer_id.hex()} does not exist")
            replacement = copy.deepcopy(endpoint)
            for index, current in enumerate(issuer.endpoints):
                if current.id == replacement.id:
                    issuer.endpoints[index] = replacement
                    break
            else:
                issuer.endpoints.append(replacement)
            self._persist_locked()

    def remove_endpoint(self, endpoint_id: bytes) -> bool:
        with self._lock:
            removed = False
            for issuer in self._issuers:
                retained = [
                    endpoint
                    for endpoint in issuer.endpoints
                    if endpoint.id != endpoint_id
                ]
                if len(retained) != len(issuer.endpoints):
                    issuer.endpoints = retained
                    removed = True
            if removed:
                self._persist_locked()
        if removed:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM endpoint_users WHERE endpoint_id = ?",
                    (endpoint_id.hex(),),
                )
                connection.execute(
                    """
                    DELETE FROM credential_users
                    WHERE credential_type = 'apple_home_key'
                      AND credential_id = ?
                    """,
                    (endpoint_id.hex(),),
                )
        return removed

    def upsert_issuers(self, issuers: list[Issuer]) -> None:
        with self._lock:
            for issuer in copy.deepcopy(issuers):
                for index, current in enumerate(self._issuers):
                    if current.id == issuer.id:
                        self._issuers[index] = issuer
                        break
                else:
                    self._issuers.append(issuer)
            self._persist_locked()

    def map_endpoint(
        self, endpoint_id: str, user_id: str, *, enabled: bool = True
    ) -> None:
        endpoint_id = endpoint_id.lower()
        if self.get_endpoint_by_id(bytes.fromhex(endpoint_id)) is None:
            raise KeyError(f"endpoint {endpoint_id} is not enrolled")
        self.map_credential(
            "apple_home_key", endpoint_id, user_id, enabled=enabled
        )

    def resolve_user(self, endpoint_id: str) -> str | None:
        return self.resolve_credential_user(
            "apple_home_key", endpoint_id
        )

    def map_credential(
        self,
        credential_type: str,
        credential_id: str,
        user_id: str,
        *,
        enabled: bool = True,
    ) -> None:
        credential_type = credential_type.lower()
        credential_id = credential_id.lower()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO credential_users(
                    credential_type, credential_id, user_id, enabled, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(credential_type, credential_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    credential_type,
                    credential_id,
                    user_id,
                    int(enabled),
                    int(time.time()),
                ),
            )

    def resolve_credential_user(
        self, credential_type: str, credential_id: str
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT user_id FROM credential_users
                WHERE credential_type = ? AND credential_id = ?
                  AND enabled = 1
                """,
                (credential_type.lower(), credential_id.lower()),
            ).fetchone()
        return None if row is None else str(row["user_id"])

    def record_event(self, event: AccessEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO access_events(
                    event_id, occurred_at, controller_id, door_id, reader_id,
                    credential_type, credential_id, endpoint_id, user_id,
                    granted, reason, api_status, duration_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.occurred_at,
                    event.controller_id,
                    event.door_id,
                    event.reader_id,
                    event.credential_type,
                    event.credential_id,
                    event.endpoint_id,
                    event.user_id,
                    int(event.granted),
                    event.reason,
                    event.api_status,
                    event.duration_ms,
                ),
            )

    def new_event(
        self,
        *,
        event_id: str | None = None,
        controller_id: str,
        door_id: str,
        reader_id: str,
        credential_type: str,
        credential_id: str,
        endpoint_id: str | None,
        user_id: str | None,
        granted: bool,
        reason: str,
        api_status: int | None,
        duration_ms: float,
    ) -> AccessEvent:
        return AccessEvent(
            event_id=event_id or str(uuid.uuid4()),
            occurred_at=int(time.time()),
            controller_id=controller_id,
            door_id=door_id,
            reader_id=reader_id,
            credential_type=credential_type,
            credential_id=credential_id,
            endpoint_id=endpoint_id,
            user_id=user_id,
            granted=granted,
            reason=reason,
            api_status=api_status,
            duration_ms=duration_ms,
        )

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM access_events
                ORDER BY occurred_at DESC, rowid DESC LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_credential_mappings(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT credential_type, credential_id, user_id, enabled
                FROM credential_users
                ORDER BY credential_type, credential_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        endpoints = self.get_all_endpoints()
        return {
            "configured": self.is_configured(),
            "reader_group_identifier": (
                self.get_reader_group_identifier().hex()
                if self.is_configured()
                else None
            ),
            "issuers": len(self.get_all_issuers()),
            "endpoints": [
                {
                    "endpoint_id": endpoint.id.hex(),
                    "last_used_at": endpoint.last_used_at,
                    "counter": endpoint.counter,
                    "user_id": self.resolve_user(endpoint.id.hex()),
                }
                for endpoint in endpoints
            ],
            "credential_mappings": self.list_credential_mappings(),
        }
