from __future__ import annotations

import hmac
import hashlib
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

from .protocol import (
    ErrorCode,
    Message,
    MessageType,
    ReaderRuntimeState,
    normalize_reader_id,
)


log = logging.getLogger(__name__)


class ReaderUnavailable(ConnectionError):
    pass


class ReaderCommandError(IOError):
    def __init__(self, code: ErrorCode):
        super().__init__(f"reader returned {code.name}")
        self.code = code


@dataclass(frozen=True)
class ReaderRecord:
    reader_id: str
    door_id: str
    token: str
    enabled: bool
    legacy_token: str | None = None

    def accepts_token(self, supplied_token: str) -> bool:
        current_matches = hmac.compare_digest(
            supplied_token, self.token
        )
        legacy_matches = (
            hmac.compare_digest(supplied_token, self.legacy_token)
            if self.legacy_token is not None
            else False
        )
        return current_matches or legacy_matches


@dataclass
class ReaderHealth:
    connected: bool = False
    state: str = "offline"
    reason: str | None = "never_connected"
    firmware: str | None = None
    connected_at: int | None = None
    last_seen: int | None = None
    pn532_ready: bool = False
    failure_count: int = 0
    retry_in_seconds: float | None = None
    wifi_rssi: int | None = None
    wifi_reconnects: int | None = None


def load_registry(path: Path) -> dict[str, ReaderRecord]:
    with path.open("r", encoding="utf-8") as source:
        document = json.load(source)
    fleet_token = str(document.get("fleet_token", "")).strip()
    records: dict[str, ReaderRecord] = {}
    for raw_reader_id, value in document.get("readers", {}).items():
        reader_id = normalize_reader_id(raw_reader_id)
        configured_token = str(value.get("token", "")).strip()
        legacy_token: str | None = None
        if fleet_token:
            token = hmac.new(
                fleet_token.encode("utf-8"),
                reader_id.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if configured_token and configured_token != token:
                legacy_token = configured_token
        elif configured_token:
            token = configured_token
        else:
            raise ValueError(
                f"reader {reader_id} has no token and registry has no "
                "fleet_token"
            )
        records[reader_id] = ReaderRecord(
            reader_id=reader_id,
            door_id=str(value["door_id"]),
            token=token,
            enabled=bool(value.get("enabled", True)),
            legacy_token=legacy_token,
        )
    return records


class ReaderConnection:
    def __init__(
        self,
        record: ReaderRecord,
        websocket: ServerConnection,
        firmware: str,
        wifi_rssi: int | None = None,
        wifi_reconnects: int | None = None,
    ) -> None:
        self.record = record
        self.websocket = websocket
        self.firmware = firmware
        self.wifi_rssi = wifi_rssi
        self.wifi_reconnects = wifi_reconnects
        self.connected_at = time.time()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[Message | BaseException]] = {}
        self._target_events: queue.Queue[bytes | BaseException] = queue.Queue(
            maxsize=1
        )
        self._next_request_id = 1
        self.closed = threading.Event()

    def _allocate_request_id(self) -> int:
        with self._pending_lock:
            request_id = self._next_request_id
            self._next_request_id = 1 if request_id == 0xFFFFFFFF else request_id + 1
            return request_id

    def request(
        self,
        message_type: MessageType,
        *,
        payload: bytes = b"",
        timeout_ms: int = 500,
    ) -> bytes:
        request_id = self._allocate_request_id()
        response_queue: queue.Queue[Message | BaseException] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = response_queue

        message = Message(
            type=message_type,
            request_id=request_id,
            timeout_ms=min(max(timeout_ms, 0), 0xFFFF),
            payload=payload,
        )
        started = time.monotonic()
        try:
            self.send(message)
            # The reader first waits for the PN532 ACK and then allows up to
            # 1.5 seconds for the complete local response. Include both
            # phases plus Wi-Fi scheduling so a late RF timeout cannot become
            # an unknown response that desynchronises the next command.
            response = response_queue.get(
                timeout=max((timeout_ms / 1000) + 3.5, 5)
            )
        except queue.Empty as error:
            raise TimeoutError(
                f"reader {self.record.reader_id} did not answer request {request_id}"
            ) from error
        except ConnectionClosed as error:
            raise ReaderUnavailable(
                f"reader {self.record.reader_id} disconnected"
            ) from error
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        if isinstance(response, BaseException):
            raise ReaderUnavailable(str(response)) from response
        if response.type == MessageType.ERROR_RESPONSE:
            code = ErrorCode(response.payload[0]) if response.payload else ErrorCode.BAD_MESSAGE
            raise ReaderCommandError(code)
        if response.type != MessageType.RESPONSE:
            raise ReaderCommandError(ErrorCode.BAD_MESSAGE)
        elapsed_ms = (time.monotonic() - started) * 1000
        # A no-target PN532 polling command commonly takes around 100-140 ms.
        # Only flag latency outside that normal operating range.
        if elapsed_ms >= 250:
            log.warning(
                "Slow reader command: reader=%s type=%s request=%d elapsed=%.1fms",
                self.record.reader_id,
                message_type.name,
                request_id,
                elapsed_ms,
            )
        return response.payload

    def send(self, message: Message) -> None:
        with self._send_lock:
            self.websocket.send(message.encode())

    def dispatch(self, message: Message) -> None:
        with self._pending_lock:
            response_queue = self._pending.get(message.request_id)
        if response_queue is None:
            log.warning(
                "Ignoring response for unknown request %d from %s",
                message.request_id,
                self.record.reader_id,
            )
            return
        response_queue.put_nowait(message)

    def dispatch_target(self, payload: bytes) -> None:
        try:
            self._target_events.put_nowait(payload)
        except queue.Full:
            log.warning(
                "Dropping duplicate target event from %s",
                self.record.reader_id,
            )

    def wait_for_target(self, timeout: float) -> bytes | None:
        try:
            event = self._target_events.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(event, BaseException):
            raise ReaderUnavailable(str(event)) from event
        return event

    def clear_target_events(self) -> None:
        while True:
            try:
                self._target_events.get_nowait()
            except queue.Empty:
                return

    def dispatch_fault(self, error: BaseException) -> None:
        self.clear_target_events()
        try:
            self._target_events.put_nowait(error)
        except queue.Full:
            pass

    def fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            queues = list(self._pending.values())
        for response_queue in queues:
            try:
                response_queue.put_nowait(error)
            except queue.Full:
                pass
        try:
            self._target_events.put_nowait(error)
        except queue.Full:
            pass
        self.closed.set()


class ReaderManager:
    def __init__(self, registry: dict[str, ReaderRecord]) -> None:
        self.registry = registry
        self._condition = threading.Condition()
        self._connections: dict[str, ReaderConnection] = {}
        self._health = {
            reader_id: ReaderHealth()
            for reader_id in self.registry
        }

    def add_record(self, record: ReaderRecord) -> ReaderRecord:
        """Add a newly authenticated reader without restarting the service."""
        with self._condition:
            existing = self.registry.get(record.reader_id)
            if existing is not None:
                return existing
            self.registry[record.reader_id] = record
            self._health[record.reader_id] = ReaderHealth()
            self._condition.notify_all()
            return record

    def get(self, reader_id: str) -> ReaderConnection | None:
        with self._condition:
            return self._connections.get(normalize_reader_id(reader_id))

    def wait_for(self, reader_id: str, timeout: float | None) -> ReaderConnection:
        reader_id = normalize_reader_id(reader_id)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while reader_id not in self._connections:
                remaining = (
                    None if deadline is None else max(0, deadline - time.monotonic())
                )
                if remaining == 0 or not self._condition.wait(remaining):
                    raise ReaderUnavailable(
                        f"reader {reader_id} did not connect within {timeout}s"
                    )
            return self._connections[reader_id]

    def register(self, connection: ReaderConnection) -> None:
        reader_id = connection.record.reader_id
        with self._condition:
            previous = self._connections.get(reader_id)
            if previous is not None:
                previous.websocket.close(code=1012, reason="reader reconnected")
            self._connections[reader_id] = connection
            now = int(time.time())
            health = self._health[reader_id]
            health.connected = True
            health.state = "initializing"
            health.reason = "pn532_initializing"
            health.firmware = connection.firmware
            health.wifi_rssi = connection.wifi_rssi
            health.wifi_reconnects = connection.wifi_reconnects
            health.connected_at = int(connection.connected_at)
            health.last_seen = now
            health.pn532_ready = False
            health.retry_in_seconds = None
            self._condition.notify_all()

    def unregister(self, connection: ReaderConnection) -> None:
        reader_id = connection.record.reader_id
        with self._condition:
            if self._connections.get(reader_id) is connection:
                del self._connections[reader_id]
                health = self._health[reader_id]
                health.connected = False
                health.state = "offline"
                health.reason = "websocket_disconnected"
                health.last_seen = int(time.time())
                health.pn532_ready = False
                health.retry_in_seconds = None
            self._condition.notify_all()

    def mark_seen(self, reader_id: str) -> None:
        reader_id = normalize_reader_id(reader_id)
        with self._condition:
            self._health[reader_id].last_seen = int(time.time())

    def mark_ready(self, reader_id: str) -> None:
        reader_id = normalize_reader_id(reader_id)
        with self._condition:
            health = self._health[reader_id]
            health.connected = reader_id in self._connections
            health.state = "online" if health.connected else "offline"
            health.reason = None if health.connected else "websocket_disconnected"
            health.pn532_ready = health.connected
            health.failure_count = 0
            health.retry_in_seconds = None
            health.last_seen = int(time.time())

    def mark_initializing(self, reader_id: str) -> None:
        reader_id = normalize_reader_id(reader_id)
        with self._condition:
            health = self._health[reader_id]
            health.connected = reader_id in self._connections
            health.state = (
                "initializing" if health.connected else "offline"
            )
            health.reason = (
                "pn532_initializing"
                if health.connected
                else "websocket_disconnected"
            )
            health.pn532_ready = False
            health.retry_in_seconds = None
            health.last_seen = int(time.time())

    def mark_fault(
        self,
        reader_id: str,
        reason: str,
        *,
        failure_count: int = 0,
        retry_in_seconds: float | None = None,
    ) -> None:
        reader_id = normalize_reader_id(reader_id)
        with self._condition:
            health = self._health[reader_id]
            health.connected = reader_id in self._connections
            health.state = "degraded" if health.connected else "offline"
            health.reason = reason
            health.pn532_ready = False
            health.failure_count = failure_count
            health.retry_in_seconds = retry_in_seconds
            health.last_seen = int(time.time())

    def reader_status(self) -> list[dict[str, Any]]:
        with self._condition:
            result = []
            for reader_id, record in sorted(self.registry.items()):
                health = self._health[reader_id]
                result.append(
                    {
                        "reader_id": reader_id,
                        "door_id": record.door_id,
                        "enabled": record.enabled,
                        "connected": health.connected,
                        "state": health.state,
                        "reason": health.reason,
                        "pn532_ready": health.pn532_ready,
                        "firmware": health.firmware,
                        "connected_at": health.connected_at,
                        "last_seen": health.last_seen,
                        "failure_count": health.failure_count,
                        "retry_in_seconds": health.retry_in_seconds,
                        "wifi_rssi": health.wifi_rssi,
                        "wifi_reconnects": health.wifi_reconnects,
                    }
                )
            return result


class ReaderWebSocketServer:
    def __init__(
        self,
        manager: ReaderManager,
        host: str = "0.0.0.0",
        port: int = 8765,
        button_handler: Callable[[ReaderRecord], bool] | None = None,
        enrollment_handler: (
            Callable[[str, str], ReaderRecord | None] | None
        ) = None,
    ) -> None:
        self.manager = manager
        self.host = host
        self.port = port
        self.button_handler = button_handler
        self.enrollment_handler = enrollment_handler
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._event_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="reader-event",
        )

    def _process_button_event(
        self,
        connection: ReaderConnection,
        request_id: int,
    ) -> None:
        delivered = False
        if self.button_handler is not None:
            try:
                delivered = bool(
                    self.button_handler(connection.record)
                )
            except Exception:
                log.exception(
                    "Button handler failed for reader %s",
                    connection.record.reader_id,
                )
        try:
            connection.send(
                Message(
                    MessageType.BUTTON_RESULT,
                    request_id=request_id,
                    payload=bytes([int(delivered)]),
                )
            )
        except ConnectionClosed:
            pass

    def _authenticate_hello(
        self, raw_hello: str | bytes
    ) -> tuple[ReaderRecord, str, int | None, int | None]:
        if not isinstance(raw_hello, str):
            raise ValueError("first message must be a JSON hello")
        hello = json.loads(raw_hello)
        if hello.get("type") != "hello" or hello.get("protocol") != 1:
            raise ValueError("unsupported hello message")
        reader_id = normalize_reader_id(str(hello.get("reader_id", "")))
        supplied_token = str(hello.get("token", ""))
        record = self.manager.registry.get(reader_id)
        if record is None and self.enrollment_handler is not None:
            record = self.enrollment_handler(reader_id, supplied_token)
        if record is None or not record.enabled:
            raise PermissionError(f"reader {reader_id} is not enabled")
        if not record.accepts_token(supplied_token):
            raise PermissionError(f"reader {reader_id} supplied an invalid token")
        wifi_rssi = hello.get("wifi_rssi")
        wifi_reconnects = hello.get("wifi_reconnects")
        return (
            record,
            str(hello.get("firmware", "unknown")),
            int(wifi_rssi) if wifi_rssi is not None else None,
            int(wifi_reconnects)
            if wifi_reconnects is not None
            else None,
        )

    def _handler(self, websocket: ServerConnection) -> None:
        connection: ReaderConnection | None = None
        try:
            raw_hello = websocket.recv(timeout=5)
            (
                record,
                firmware,
                wifi_rssi,
                wifi_reconnects,
            ) = self._authenticate_hello(raw_hello)
            connection = ReaderConnection(
                record,
                websocket,
                firmware,
                wifi_rssi=wifi_rssi,
                wifi_reconnects=wifi_reconnects,
            )
            self.manager.register(connection)
            log.info(
                "Reader %s connected for door %s (firmware %s)",
                record.reader_id,
                record.door_id,
                firmware,
            )
            for raw_message in websocket:
                if not isinstance(raw_message, bytes):
                    raise ValueError("reader response must be binary")
                message = Message.decode(raw_message)
                self.manager.mark_seen(record.reader_id)
                if message.type == MessageType.BUTTON_EVENT:
                    if message.payload:
                        raise ValueError(
                            "button event payload must be empty"
                        )
                    self._event_executor.submit(
                        self._process_button_event,
                        connection,
                        message.request_id,
                    )
                elif message.type == MessageType.TARGET_EVENT:
                    if not message.payload:
                        raise ValueError(
                            "target event payload must not be empty"
                        )
                    connection.dispatch_target(message.payload)
                elif message.type == MessageType.READER_STATUS:
                    if len(message.payload) != 3:
                        raise ValueError(
                            "reader status payload must contain 3 bytes"
                        )
                    state = ReaderRuntimeState(message.payload[0])
                    failure_count = message.payload[2]
                    if state == ReaderRuntimeState.READY:
                        self.manager.mark_ready(record.reader_id)
                    else:
                        try:
                            error_name = ErrorCode(
                                message.payload[1]
                            ).name.lower()
                        except ValueError:
                            error_name = (
                                f"unknown_{message.payload[1]:02x}"
                            )
                        reason = (
                            error_name
                            if error_name.startswith("pn532_")
                            else f"pn532_{error_name}"
                        )
                        self.manager.mark_fault(
                            record.reader_id,
                            reason,
                            failure_count=failure_count,
                        )
                        connection.dispatch_fault(
                            ReaderUnavailable(reason)
                        )
                else:
                    connection.dispatch(message)
        except ConnectionClosed as error:
            if connection is not None:
                log.warning(
                    "Reader %s WebSocket closed: %s",
                    connection.record.reader_id,
                    error,
                )
        except TimeoutError:
            if connection is not None:
                log.warning(
                    "Reader %s WebSocket timed out",
                    connection.record.reader_id,
                )
        except (PermissionError, ValueError) as error:
            log.warning("Reader connection rejected: %s", error)
            try:
                websocket.close(code=1008, reason="invalid reader connection")
            except ConnectionClosed:
                pass
        except Exception:
            log.exception("Reader connection rejected or failed")
            try:
                websocket.close(code=1008, reason="invalid reader connection")
            except ConnectionClosed:
                pass
        finally:
            if connection is not None:
                connection.fail_pending(
                    ReaderUnavailable(
                        f"reader {connection.record.reader_id} disconnected"
                    )
                )
                self.manager.unregister(connection)
                log.info("Reader %s disconnected", connection.record.reader_id)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._server = serve(
            self._handler,
            self.host,
            self.port,
            compression=None,
            ping_interval=20,
            ping_timeout=15,
            max_size=2048,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="homekey-reader-websocket",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self._event_executor.shutdown(wait=True, cancel_futures=True)
