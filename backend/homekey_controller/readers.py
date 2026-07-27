from __future__ import annotations

import errno
import importlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass

from homekey_bridge.protocol import ErrorCode, MessageType, normalize_reader_id
from homekey_bridge.server import (
    ReaderCommandError,
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
from util.nfc import with_crc16a  # noqa: E402


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
            original_exchange = device.send_cmd_recv_rsp

            def send_cmd_recv_rsp(target, data, timeout):
                if (
                    target.brty == "106A"
                    and target.sel_res
                    and target.sel_res[0] & 0x20
                ):
                    try:
                        status, response = transport.transceive(
                            data, timeout
                        )
                    except ReaderCommandError as error:
                        if error.code == ErrorCode.UNSUPPORTED_TYPE:
                            return original_exchange(
                                target, data, timeout
                            )
                        raise
                    if status == 0:
                        return response
                    if status == 1:
                        raise nfc.clf.TimeoutError
                    if status == 0x0B:
                        # PN532 reports that the ISO-DEP protocol state is
                        # broken. NAK retransmissions cannot repair this; end
                        # the session so discovery can perform a clean retry.
                        raise nfc.clf.ProtocolError(
                            "PN532 RF protocol error"
                        )
                    raise nfc.clf.TransmissionError(
                        f"PN532 RF status 0x{status:02X}"
                    )
                return original_exchange(target, data, timeout)

            device.send_cmd_recv_rsp = send_cmd_recv_rsp
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
        self._autonomous_discovery = False

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
        broadcast = ECP.home(
            identifier=self.store.get_reader_group_identifier(),
            flag_2=True,
        ).pack()
        if self._autonomous_discovery:
            connection = self.manager.get(self.reader_id)
            if connection is None:
                raise ReaderUnavailable(
                    f"reader {self.reader_id} is disconnected"
                )
            discovery = connection.wait_for_target(timeout=0.5)
            if discovery is None:
                return
            remote_target = self._decode_discovery(discovery)
            self.clf.target = remote_target
        else:
            try:
                connection = self.manager.get(self.reader_id)
                if connection is None:
                    raise ReaderUnavailable(
                        f"reader {self.reader_id} is disconnected"
                    )
                discovery = connection.request(
                    MessageType.DISCOVER,
                    payload=with_crc16a(broadcast),
                    timeout_ms=1500,
                )
                remote_target = self._decode_discovery(discovery)
                self.clf.target = remote_target
            except ReaderCommandError as error:
                # Firmware before 2.6.0 does not have local ECP discovery.
                if error.code != ErrorCode.UNSUPPORTED_TYPE:
                    raise
                remote_target = self.clf.sense(
                    RemoteTarget("106A"),
                    broadcast=broadcast,
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

        target = None
        transaction_completed = False
        try:
            target = activate(self.clf, remote_target)
            if target is None:
                return
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
                transaction_completed = True
            else:
                try:
                    result = self._authenticate(target)
                    if result is not None:
                        self._decide_access(result)
                        transaction_completed = True
                except ProtocolError as error:
                    log.info(
                        "Reader %s rejected Home Key protocol: %s",
                        self.reader_id,
                        error,
                    )
        finally:
            if self._autonomous_discovery:
                connection = self.manager.get(self.reader_id)
                if connection is not None:
                    connection.request(
                        MessageType.RESUME_DISCOVERY,
                        payload=bytes(
                            [0 if transaction_completed else 1]
                        ),
                        timeout_ms=500,
                    )
            else:
                # This must also run when ISO-DEP raises. Retrying while the
                # same phone remains in the field causes a rapid sequence of
                # broken sessions and makes RF recovery substantially worse.
                if target is not None:
                    try:
                        while (
                            not self.stop_event.is_set()
                            and target.is_present
                        ):
                            time.sleep(0.25)
                    except Exception:
                        pass
                time.sleep(0.5)

    def _start_autonomous_discovery(self) -> bool:
        connection = self.manager.get(self.reader_id)
        if connection is None:
            raise ReaderUnavailable(
                f"reader {self.reader_id} is disconnected"
            )
        broadcast = ECP.home(
            identifier=self.store.get_reader_group_identifier(),
            flag_2=True,
        ).pack()
        try:
            connection.clear_target_events()
            connection.request(
                MessageType.START_DISCOVERY,
                payload=with_crc16a(broadcast),
                timeout_ms=500,
            )
        except ReaderCommandError as error:
            if error.code == ErrorCode.UNSUPPORTED_TYPE:
                return False
            raise
        return True

    @staticmethod
    def _decode_discovery(payload: bytes) -> RemoteTarget | None:
        """Decode a local PN532 InListPassiveTarget discovery result."""
        if payload == b"\x00":
            return None
        if len(payload) < 8 or payload[0] != 1:
            raise IOError("reader returned an invalid discovery response")

        # Remaining bytes are the PN532 InListPassiveTarget response:
        # NbTg, Tg, SENS_RES[2], SEL_RES, NFCIDLength, NFCID, ATS...
        response = payload[1:]
        if response[0] < 1 or response[1] != 1:
            raise IOError("reader returned an invalid Type-A target")
        target_data = response[2:]
        uid_length = target_data[3]
        if uid_length not in (4, 7, 10):
            raise IOError("reader returned an invalid Type-A UID")
        if len(target_data) < 4 + uid_length:
            raise IOError("reader returned a truncated Type-A target")
        return RemoteTarget(
            "106A",
            sens_res=target_data[1::-1],
            sel_res=target_data[2:3],
            sdd_res=target_data[4:],
        )

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
        self._autonomous_discovery = self._start_autonomous_discovery()
        if self._autonomous_discovery:
            log.info(
                "Reader %s autonomous discovery active",
                self.reader_id,
            )
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
