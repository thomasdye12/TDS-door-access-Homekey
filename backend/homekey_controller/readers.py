from __future__ import annotations

import errno
import importlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass

from homekey_bridge.protocol import MessageType, normalize_reader_id
from homekey_bridge.server import (
    ReaderManager,
    ReaderUnavailable,
)
from homekey_bridge.transport import WebSocketPn532Transport

from .access_api import AccessApiClient, AccessDecision
from .config import ControllerConfig
from .store import SQLiteKeyStore
from .vendor import activate_vendor

activate_vendor()

import nfc  # noqa: E402
from homekey import ProtocolError, read_homekey  # noqa: E402
from util.bfclf import (  # noqa: E402
    BroadcastFrameContactlessFrontend,
    ISODEPTag,
    RemoteTarget,
    activate,
)
from util.digital_key import (  # noqa: E402
    DigitalKeyFlow,
    DigitalKeyTransactionType,
)
from util.ecp import ECP  # noqa: E402
from util.iso7816 import ISO7816Tag  # noqa: E402


log = logging.getLogger(__name__)


def install_websocket_nfc_transport(manager: ReaderManager) -> None:
    """Teach nfcpy to open ws-pn532:<MAC> paths for all readers."""
    nfc_device = importlib.import_module("nfc.clf.device")
    pn532_module = importlib.import_module("nfc.clf.pn532")
    if getattr(nfc_device.connect, "_homekey_controller_patch", False):
        return
    original_connect = nfc_device.connect

    def connect(path: str):
        prefix = "ws-pn532:"
        if path.startswith(prefix):
            reader_id = normalize_reader_id(path[len(prefix) :])
            transport = WebSocketPn532Transport(manager, reader_id)
            device = pn532_module.init(transport)
            device._path = path
            return device
        return original_connect(path)

    connect._homekey_controller_patch = True
    nfc_device.connect = connect


@dataclass(frozen=True)
class CredentialResult:
    credential_type: str
    credential_id: str
    endpoint_id: str | None
    flow: str
    transaction_ms: float


class ReaderWorker:
    def __init__(
        self,
        *,
        reader_id: str,
        manager: ReaderManager,
        store: SQLiteKeyStore,
        access_api: AccessApiClient,
        config: ControllerConfig,
        authentication_lock: threading.Lock,
    ) -> None:
        self.reader_id = normalize_reader_id(reader_id)
        self.manager = manager
        self.store = store
        self.access_api = access_api
        self.config = config
        self.authentication_lock = authentication_lock
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"reader-{self.reader_id}",
            daemon=True,
        )
        self.clf = BroadcastFrameContactlessFrontend(
            path=f"ws-pn532:{self.reader_id}",
            broadcast_enabled=True,
        )
        self._consecutive_failures = 0

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.clf.close()
        except Exception:
            pass

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def _authenticate(self, target: ISODEPTag) -> CredentialResult | None:
        started = time.monotonic()
        tag = ISO7816Tag(target)
        # One shared logical lock means endpoint counters and persistent-key
        # updates must be ordered across all physical readers.
        with self.authentication_lock:
            flow, new_issuers, endpoint = read_homekey(
                tag,
                issuers=self.store.get_all_issuers(),
                preferred_versions=[b"\x02\x00"],
                flow=DigitalKeyFlow.FAST,
                transaction_code=DigitalKeyTransactionType.UNLOCK,
                reader_identifier=(
                    self.store.get_reader_group_identifier()
                    + self.store.get_reader_identifier()
                ),
                reader_private_key=self.store.get_reader_private_key(),
                key_size=16,
            )
            if new_issuers:
                self.store.upsert_issuers(new_issuers)
        if endpoint is None:
            return None
        return CredentialResult(
            credential_type="apple_home_key",
            credential_id=endpoint.id.hex(),
            endpoint_id=endpoint.id.hex(),
            flow=flow.name.lower(),
            transaction_ms=(time.monotonic() - started) * 1000,
        )

    def _decide_access(self, result: CredentialResult) -> None:
        event_id = str(uuid.uuid4())
        user_id = self.store.resolve_credential_user(
            result.credential_type, result.credential_id
        )
        # The Homeserver access endpoint calls this field user_id but uses it
        # as the credential/fob lookup value. If no explicit mapping exists,
        # submit the stable Home Key endpoint ID directly. Legacy RFID records
        # use the UID bytes as one unsigned decimal integer, matching the old
        # MFRC522 sketch's repeated left-shift operation.
        if user_id:
            api_user_id = user_id
        elif result.credential_type == "rfid_uid":
            api_user_id = str(int(result.credential_id, 16))
        else:
            api_user_id = result.credential_id
        payload = {
            "event_id": event_id,
            "controller_id": self.config.controller_id,
            "door_id": self.config.door_id,
            "reader_id": self.reader_id,
            "endpoint_id": result.endpoint_id,
            "credential_id": result.credential_id,
            "user_id": api_user_id,
            "credential": result.credential_type,
            "authentication_flow": result.flow,
            "authenticated_at": int(time.time()),
            "timestamp": int(time.time()),
            "authentication_ms": round(result.transaction_ms, 3),
        }
        if result.credential_type == "rfid_uid":
            payload["card_uid"] = api_user_id
            payload["card_uid_hex"] = result.credential_id
        # The access API may use the endpoint ID as its own credential lookup
        # and return a user_id. A local endpoint mapping is therefore useful
        # but not required.
        decision = self.access_api.decide(payload)
        resolved_user_id = decision.user_id or user_id

        event = self.store.new_event(
            event_id=event_id,
            controller_id=self.config.controller_id,
            door_id=self.config.door_id,
            reader_id=self.reader_id,
            credential_type=result.credential_type,
            credential_id=result.credential_id,
            endpoint_id=result.endpoint_id,
            user_id=resolved_user_id,
            granted=decision.granted,
            reason=decision.reason,
            api_status=decision.http_status,
            duration_ms=decision.duration_ms,
        )
        self.store.record_event(event)
        connection = self.manager.get(self.reader_id)
        if connection is not None:
            feedback = bytes([int(decision.granted)]) + min(
                decision.unlock_ms, 0xFFFFFFFF
            ).to_bytes(4, "big")
            try:
                connection.request(
                    MessageType.ACCESS_RESULT,
                    payload=feedback,
                    timeout_ms=500,
                )
            except Exception as error:
                # The access decision is already durable and the door API has
                # already acted. Losing optional ESP feedback must not turn a
                # grant into a second API request.
                log.warning(
                    "Could not deliver access feedback to reader %s: %s",
                    self.reader_id,
                    error,
                )
        log.info(
            "Access %s reader=%s door=%s credential=%s:%s "
            "user=%s reason=%s",
            "GRANTED" if decision.granted else "DENIED",
            self.reader_id,
            self.config.door_id,
            result.credential_type,
            result.credential_id,
            resolved_user_id or "unmapped",
            decision.reason,
        )

    def _poll_once(self) -> None:
        started = time.monotonic()
        remote_target = self.clf.sense(
            RemoteTarget("106A"),
            broadcast=ECP.home(
                identifier=self.store.get_reader_group_identifier(),
                flag_2=True,
            ).pack(),
        )
        if remote_target is None:
            time.sleep(
                max(
                    0,
                    self.config.throttle_polling
                    - time.monotonic()
                    + started,
                )
            )
            return

        target = activate(self.clf, remote_target)
        if target is None:
            return
        try:
            if not isinstance(target, ISODEPTag):
                card_uid = target.identifier.hex().upper()
                log.info(
                    "Reader %s found RFID/NFC UID %s",
                    self.reader_id,
                    card_uid,
                )
                self._decide_access(
                    CredentialResult(
                        credential_type="rfid_uid",
                        credential_id=card_uid,
                        endpoint_id=None,
                        flow="uid",
                        transaction_ms=(
                            time.monotonic() - started
                        ) * 1000,
                    )
                )
            else:
                try:
                    result = self._authenticate(target)
                    if result is not None:
                        self._decide_access(result)
                except ProtocolError as error:
                    log.info(
                        "Reader %s rejected Home Key protocol: %s",
                        self.reader_id,
                        error,
                    )
        finally:
            # This must also run when ISO-DEP raises. Retrying while the same
            # phone remains in the field causes a rapid sequence of broken
            # sessions and makes RF recovery substantially worse.
            try:
                while (
                    not self.stop_event.is_set()
                    and target.is_present
                ):
                    time.sleep(0.25)
            except Exception:
                pass
            time.sleep(0.5)

    def _is_transient(self, error: BaseException) -> bool:
        if isinstance(
            error, (nfc.clf.CommunicationError, nfc.tag.TagCommandError)
        ):
            return True
        return isinstance(error, OSError) and error.errno in (
            errno.EIO,
            errno.ETIMEDOUT,
        )

    def _reader_session(self) -> None:
        self.manager.wait_for(self.reader_id, timeout=None)
        self.clf.device = None
        self.clf.open(self.clf.path)
        if self.clf.device is None:
            raise ReaderUnavailable(
                f"PN532 {self.reader_id} did not initialize"
            )
        log.info("Reader %s PN532 ready", self.reader_id)
        while not self.stop_event.is_set():
            try:
                self._poll_once()
                self._consecutive_failures = 0
            except Exception as error:
                if not self._is_transient(error):
                    raise
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    raise
                log.warning(
                    "Reader %s transient NFC failure (%d/3): %s",
                    self.reader_id,
                    self._consecutive_failures,
                    error,
                )
                # A failed ISO-DEP session can make presence detection
                # unreliable. Give the phone time to leave the field (or its
                # NFC stack time to reset) before starting another SELECT.
                # Without this guard the same presentation can be retried
                # within a few hundred milliseconds and degrade RF recovery.
                self.stop_event.wait(1.0)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if not self.store.is_configured():
                    log.info(
                        "Reader %s waiting for HomeKit key registration",
                        self.reader_id,
                    )
                    self.stop_event.wait(2)
                    continue
                self._reader_session()
            except ReaderUnavailable as error:
                log.warning("Reader %s offline: %s", self.reader_id, error)
            except Exception:
                log.exception(
                    "Reader %s failed; reinitializing in 2 seconds",
                    self.reader_id,
                )
            finally:
                try:
                    self.clf.close()
                except Exception:
                    pass
            self.stop_event.wait(2)
