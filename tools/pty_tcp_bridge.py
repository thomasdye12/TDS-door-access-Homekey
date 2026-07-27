#!/usr/bin/env python3
"""Expose a TCP-connected PN532 bridge as a local macOS pseudo-terminal."""

from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import selectors
import signal
import socket
import sys
import termios
import time
import tty
from pathlib import Path


class PtyTcpBridge:
    def __init__(
        self,
        host: str,
        port: int,
        reconnect_delay: float = 1.0,
        connect_timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.reconnect_delay = reconnect_delay
        self.connect_timeout = connect_timeout
        self.master_fd, self.slave_fd = pty.openpty()
        tty.setraw(self.master_fd)
        tty.setraw(self.slave_fd)
        self.tty_path = os.ttyname(self.slave_fd)
        self.socket: socket.socket | None = None
        self.running = True

    @property
    def nfcpy_path(self) -> str:
        return f"tty:{Path(self.tty_path).name}:pn532"

    def stop(self, *_args: object) -> None:
        self.running = False

    def _connect(self) -> bool:
        try:
            connection = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.setblocking(False)
            self.socket = connection
            print(f"Connected to {self.host}:{self.port}", flush=True)
            return True
        except OSError as error:
            print(
                f"Waiting for {self.host}:{self.port}: {error}",
                file=sys.stderr,
                flush=True,
            )
            return False

    def _disconnect(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None
            print("Bridge disconnected; reconnecting", file=sys.stderr, flush=True)

    @staticmethod
    def _write_all(fd_or_socket: int | socket.socket, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if isinstance(fd_or_socket, socket.socket):
                written = fd_or_socket.send(view)
            else:
                written = os.write(fd_or_socket, view)
            if written == 0:
                raise ConnectionError("destination closed")
            view = view[written:]

    def _serve_connection(self) -> None:
        assert self.socket is not None
        selector = selectors.DefaultSelector()
        selector.register(self.master_fd, selectors.EVENT_READ, "pty")
        selector.register(self.socket, selectors.EVENT_READ, "tcp")

        try:
            while self.running and self.socket is not None:
                for key, _mask in selector.select(timeout=0.5):
                    if key.data == "pty":
                        try:
                            data = os.read(self.master_fd, 4096)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                continue
                            raise
                        if data:
                            self._write_all(self.socket, data)
                    else:
                        data = self.socket.recv(4096)
                        if not data:
                            raise ConnectionError("ESP8266 closed the connection")
                        self._write_all(self.master_fd, data)
        finally:
            selector.close()

    def run(self) -> None:
        print(f"Pseudo-terminal: {self.tty_path}", flush=True)
        print(f'nfcpy path:      "{self.nfcpy_path}"', flush=True)
        print("Keep this process running while the Home Key reader runs.", flush=True)

        try:
            while self.running:
                if not self._connect():
                    time.sleep(self.reconnect_delay)
                    continue
                try:
                    self._serve_connection()
                except (ConnectionError, OSError) as error:
                    if self.running:
                        print(f"Connection lost: {error}", file=sys.stderr, flush=True)
                finally:
                    self._disconnect()
                if self.running:
                    time.sleep(self.reconnect_delay)
        finally:
            self._disconnect()
            os.close(self.master_fd)
            os.close(self.slave_fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bridge an ESP8266 PN532 TCP stream to a local pseudo-TTY."
    )
    parser.add_argument("host", help="Bridge IP, hostname, or mDNS name")
    parser.add_argument("--port", type=int, default=7331, help="TCP port (default: 7331)")
    parser.add_argument(
        "--runtime-file",
        type=Path,
        help="Optionally write the generated TTY and nfcpy path as JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bridge = PtyTcpBridge(args.host, args.port)

    if args.runtime_file:
        args.runtime_file.write_text(
            json.dumps(
                {
                    "host": args.host,
                    "port": args.port,
                    "tty": bridge.tty_path,
                    "nfcpy_path": bridge.nfcpy_path,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    signal.signal(signal.SIGINT, bridge.stop)
    signal.signal(signal.SIGTERM, bridge.stop)
    bridge.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

