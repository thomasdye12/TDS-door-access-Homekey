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

from .protocol import ErrorCode, Message, MessageType, normalize_reader_id


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
    ) -> None:
        self.record = record
        self.websocket = websocket
        self.firmware = firmware
        self.connected_at = time.time()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[Message | BaseException]] = {}
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

    def fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            queues = list(self._pending.values())
        for response_queue in queues:
            try:
                response_queue.put_nowait(error)
            except queue.Full:
                pass
        self.closed.set()


class ReaderManager:
    def __init__(self, registry: dict[str, ReaderRecord]) -> None:
        self.registry = registry
        self._condition = threading.Condition()
        self._connections: dict[str, ReaderConnection] = {}

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
            self._condition.notify_all()

    def unregister(self, connection: ReaderConnection) -> None:
        reader_id = connection.record.reader_id
        with self._condition:
            if self._connections.get(reader_id) is connection:
                del self._connections[reader_id]
            self._condition.notify_all()


class ReaderWebSocketServer:
    def __init__(
        self,
        manager: ReaderManager,
        host: str = "0.0.0.0",
        port: int = 8765,
        button_handler: Callable[[ReaderRecord], bool] | None = None,
    ) -> None:
        self.manager = manager
        self.host = host
        self.port = port
        self.button_handler = button_handler
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
    ) -> tuple[ReaderRecord, str]:
        if not isinstance(raw_hello, str):
            raise ValueError("first message must be a JSON hello")
        hello = json.loads(raw_hello)
        if hello.get("type") != "hello" or hello.get("protocol") != 1:
            raise ValueError("unsupported hello message")
        reader_id = normalize_reader_id(str(hello.get("reader_id", "")))
        record = self.manager.registry.get(reader_id)
        if record is None or not record.enabled:
            raise PermissionError(f"reader {reader_id} is not enabled")
        supplied_token = str(hello.get("token", ""))
        if not record.accepts_token(supplied_token):
            raise PermissionError(f"reader {reader_id} supplied an invalid token")
        return record, str(hello.get("firmware", "unknown"))

    def _handler(self, websocket: ServerConnection) -> None:
        connection: ReaderConnection | None = None
        try:
            raw_hello = websocket.recv(timeout=5)
            record, firmware = self._authenticate_hello(raw_hello)
            connection = ReaderConnection(record, websocket, firmware)
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
                else:
                    connection.dispatch(message)
        except (ConnectionClosed, TimeoutError):
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
            ping_interval=10,
            ping_timeout=5,
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
