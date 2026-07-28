from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from homekey_bridge.protocol import ErrorCode, MessageType
from homekey_bridge.server import ReaderCommandError
from homekey_controller.access_api import AccessDecision
from homekey_controller.readers import CredentialResult, ReaderWorker
from homekey_controller.store import AccessEvent


class FakeConnection:
    def __init__(self) -> None:
        self.requests = []

    def request(
        self,
        message_type,
        *,
        payload=b"",
        timeout_ms=500,
    ):
        self.requests.append((message_type, payload, timeout_ms))
        return b""


class FakeManager:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def get(self, _reader_id):
        return self.connection


class FakeApi:
    def __init__(self) -> None:
        self.payload = None

    def decide(self, payload):
        self.payload = payload
        return AccessDecision(
            granted=True,
            reason="card_allowed",
            unlock_ms=4000,
            user_id="api-user",
            http_status=200,
            duration_ms=10,
        )


class FakeStore:
    def __init__(self) -> None:
        self.event = None

    def resolve_credential_user(self, _type, _identifier):
        return None

    def new_event(self, **values):
        return AccessEvent(occurred_at=1, **values)

    def record_event(self, event):
        self.event = event


class ReaderDecisionTests(unittest.TestCase):
    def test_pn532_failure_reason_is_not_double_prefixed(self) -> None:
        reason = ReaderWorker._failure_reason(
            ReaderCommandError(ErrorCode.PN532_TIMEOUT)
        )
        self.assertEqual(reason, "pn532_timeout")

    def test_decodes_local_type_a_discovery(self) -> None:
        # status, NbTg, Tg, SENS_RES, SEL_RES, UID length, UID
        payload = bytes.fromhex("0101014400200404A1B2C3")
        target = ReaderWorker._decode_discovery(payload)

        self.assertIsNotNone(target)
        self.assertEqual(bytes(target.sens_res), bytes.fromhex("0044"))
        self.assertEqual(bytes(target.sel_res), bytes.fromhex("20"))
        self.assertEqual(bytes(target.sdd_res), bytes.fromhex("04A1B2C3"))

    def test_decodes_no_local_target(self) -> None:
        self.assertIsNone(ReaderWorker._decode_discovery(b"\x00"))

    def test_rfid_uid_uses_api_audit_and_reader_feedback(self) -> None:
        manager = FakeManager()
        store = FakeStore()
        api = FakeApi()
        config = SimpleNamespace(
            controller_id="controller",
            door_id="door",
            throttle_polling=0.1,
        )
        worker = ReaderWorker(
            reader_id="c8:c9:a3:38:59:af",
            manager=manager,
            store=store,
            access_api=api,
            config=config,
            authentication_lock=threading.Lock(),
        )

        worker._decide_access(
            CredentialResult(
                credential_type="rfid_uid",
                credential_id="04A1B2C3",
                endpoint_id=None,
                flow="uid",
                transaction_ms=12,
            )
        )

        self.assertEqual(api.payload["credential"], "rfid_uid")
        self.assertEqual(api.payload["card_uid"], "77705923")
        self.assertEqual(api.payload["card_uid_hex"], "04A1B2C3")
        self.assertEqual(api.payload["user_id"], "77705923")
        self.assertEqual(store.event.credential_id, "04A1B2C3")
        self.assertEqual(store.event.user_id, "api-user")
        self.assertEqual(
            manager.connection.requests,
            [
                (
                    MessageType.ACCESS_RESULT,
                    b"\x01\x00\x00\x0f\xa0",
                    500,
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
