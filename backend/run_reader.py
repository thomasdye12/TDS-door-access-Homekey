#!/usr/bin/env python3
"""Run the existing Home Key reader with a direct WebSocket PN532 transport."""

from __future__ import annotations

import argparse
import errno
import importlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from homekey_bridge.protocol import normalize_reader_id
from homekey_bridge.server import (
    ReaderManager,
    ReaderUnavailable,
    ReaderWebSocketServer,
    load_registry,
)
from homekey_bridge.transport import WebSocketPn532Transport


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reader-id",
        required=True,
        help="NodeMCU station MAC, with or without separators",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path(__file__).parent / "config" / "readers.json",
    )
    parser.add_argument(
        "--reader-project",
        type=Path,
        required=True,
        help="Path to the legacy apple-home-key-reader checkout",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--connect-timeout", type=float, default=60)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose Home Key and WebSocket diagnostics",
    )
    return parser.parse_args()


def add_reader_environment(reader_project: Path) -> None:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = reader_project / "venv" / "lib" / version / "site-packages"
    if not site_packages.is_dir():
        raise SystemExit(f"Reader virtualenv packages not found: {site_packages}")
    sys.path.insert(0, str(site_packages))
    sys.path.insert(0, str(reader_project))


def main() -> int:
    args = parse_args()
    # The existing Home Key application installs its own root handler. Avoid a
    # second handler (duplicate lines), and suppress WebSocket payload dumps
    # even when the original application runs with DEBUG logging.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("homekey_bridge").setLevel(logging.INFO)

    reader_id = normalize_reader_id(args.reader_id)
    reader_project = args.reader_project.expanduser().resolve()
    configuration_path = reader_project / "configuration.json"
    if not configuration_path.is_file():
        raise SystemExit(f"Reader configuration not found: {configuration_path}")

    registry = load_registry(args.registry)
    if reader_id not in registry:
        raise SystemExit(f"Reader {reader_id} is not present in {args.registry}")

    manager = ReaderManager(registry)
    websocket_server = ReaderWebSocketServer(manager, args.host, args.port)
    websocket_server.start()
    print(
        f"Waiting for reader {reader_id} on ws://{args.host}:{args.port}/readers",
        flush=True,
    )
    try:
        connection = manager.wait_for(reader_id, args.connect_timeout)
    except ReaderUnavailable as error:
        websocket_server.stop()
        raise SystemExit(str(error)) from error

    print(
        f"Reader {reader_id} connected for door {connection.record.door_id}",
        flush=True,
    )

    with configuration_path.open("r", encoding="utf-8") as source:
        configuration = json.load(source)
    configuration["nfc"]["path"] = f"ws-pn532:{reader_id}"
    if not args.debug:
        configuration.setdefault("logging", {})["level"] = logging.INFO

    add_reader_environment(reader_project)
    nfc = importlib.import_module("nfc")
    nfc_device = importlib.import_module("nfc.clf.device")
    pn532_module = importlib.import_module("nfc.clf.pn532")
    service_module = importlib.import_module("service")
    original_connect = nfc_device.connect
    original_read_homekey = service_module.Service._read_homekey

    def connect(path: str):
        if path == f"ws-pn532:{reader_id}":
            transport = WebSocketPn532Transport(manager, reader_id)
            device = pn532_module.init(transport)
            device._path = path
            return device
        return original_connect(path)

    nfc_device.connect = connect

    def recover_transient_reader_error(service, error: BaseException) -> bool:
        failures = getattr(service, "_bridge_transient_failures", 0) + 1
        service._bridge_transient_failures = failures
        if failures >= 3:
            service._bridge_transient_failures = 0
            logging.getLogger("homekey_bridge.reader").error(
                "Reader failed %d consecutive exchanges; reinitializing PN532",
                failures,
            )
            return False
        logging.getLogger("homekey_bridge.reader").warning(
            "Transient NFC exchange failed (%s); keeping reader online "
            "(attempt %d/3)",
            error,
            failures,
        )
        time.sleep(0.1)
        return True

    def read_homekey_with_radio_recovery(service):
        try:
            result = original_read_homekey(service)
            service._bridge_transient_failures = 0
            return result
        except (nfc.clf.CommunicationError, nfc.tag.TagCommandError) as error:
            # A phone moving at the edge of the antenna can exhaust nfcpy's
            # ISO-DEP retries. That is a failed presentation, not a failed
            # PN532 or WebSocket connection, so keep polling instead of
            # tearing the reader down for five seconds.
            if recover_transient_reader_error(service, error):
                return None
            raise
        except OSError as error:
            # pn53x.command converts a transport PN532_TIMEOUT into EIO while
            # reporting "input/output error while waiting for ack".
            if (
                error.errno in (errno.EIO, errno.ETIMEDOUT)
                and recover_transient_reader_error(service, error)
            ):
                return None
            raise

    service_module.Service._read_homekey = read_homekey_with_radio_recovery

    os.chdir(reader_project)
    reader_main = importlib.import_module("main")
    reader_main.load_configuration = lambda _path="configuration.json": configuration
    print(
        f"Starting unchanged Home Key reader via MAC {reader_id}",
        flush=True,
    )
    try:
        reader_main.main()
    finally:
        websocket_server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
