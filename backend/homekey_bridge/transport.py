from __future__ import annotations

import threading

from .protocol import MessageType, normalize_reader_id
from .server import ReaderManager


class WebSocketPn532Transport:
    """nfcpy PN532 transport backed directly by a WebSocket reader."""

    TYPE = "TTY"

    def __init__(self, manager: ReaderManager, reader_id: str) -> None:
        self.manager = manager
        self.reader_id = normalize_reader_id(reader_id)
        self._pending_write: bytes | None = None
        self._cached_response: bytes | None = None
        self._lock = threading.Lock()
        self._baudrate = 115200

    @property
    def port(self) -> str:
        return f"ws-pn532:{self.reader_id}"

    @property
    def baudrate(self) -> int:
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value: int) -> None:
        self._baudrate = value

    def open(self, _port: str, baudrate: int = 115200) -> None:
        with self._lock:
            self._baudrate = baudrate
            self._pending_write = None
            self._cached_response = None
        connection = self.manager.wait_for(self.reader_id, timeout=10)
        connection.request(MessageType.RESET, timeout_ms=500)

    def close(self) -> None:
        with self._lock:
            self._pending_write = None
            self._cached_response = None

    def write(self, frame: bytes | bytearray) -> None:
        with self._lock:
            self._pending_write = bytes(frame)

    def read(self, timeout: int) -> bytearray:
        timeout = max(int(timeout), 250)
        connection = self.manager.wait_for(self.reader_id, timeout=5)
        with self._lock:
            if self._pending_write is not None:
                command = self._pending_write
                self._pending_write = None
                combined = connection.request(
                    MessageType.EXECUTE,
                    payload=command,
                    timeout_ms=timeout,
                )
                if len(combined) < 6:
                    raise IOError("reader returned an incomplete PN532 exchange")
                acknowledgement = combined[:6]
                self._cached_response = combined[6:]
                return bytearray(acknowledgement)
            if self._cached_response is None:
                raise IOError("PN532 read requested without a pending command")
            response = self._cached_response
            self._cached_response = None
            return bytearray(response)

    def transceive(
        self, data: bytes | bytearray, timeout: float
    ) -> tuple[int, bytearray]:
        timeout_ms = min(max(int(timeout * 1000), 1), 0xFFFF)
        connection = self.manager.wait_for(self.reader_id, timeout=5)
        response = connection.request(
            MessageType.TRANSCEIVE,
            payload=bytes(data),
            timeout_ms=timeout_ms,
        )
        if not response:
            raise IOError("reader returned an empty RF response")
        return response[0], bytearray(response[1:])
