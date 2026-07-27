#!/usr/bin/env python3
"""Run apple-home-key-reader through a bridge PTY without editing its files."""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("tty", help="PTY printed by pty_tcp_bridge.py")
    parser.add_argument(
        "--reader-project",
        type=Path,
        required=True,
        help="Path to the legacy apple-home-key-reader checkout",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the temporary NFC path without starting",
    )
    parser.add_argument(
        "--minimum-read-timeout-ms",
        type=int,
        default=500,
        help="Minimum nfcpy TTY read timeout for the network bridge (default: 500)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reader_project = args.reader_project.expanduser().resolve()
    tty_path = Path(args.tty)
    configuration_path = reader_project / "configuration.json"

    if not tty_path.exists():
        raise SystemExit(f"Pseudo-terminal does not exist: {tty_path}")
    if not configuration_path.is_file():
        raise SystemExit(f"Reader configuration not found: {configuration_path}")

    with configuration_path.open("r", encoding="utf-8") as source:
        configuration = json.load(source)

    nfcpy_path = f"tty:{tty_path.name}:pn532"
    configuration["nfc"]["path"] = nfcpy_path
    print(f"Using temporary nfcpy path: {nfcpy_path}", flush=True)
    print(f"Original configuration remains unchanged: {configuration_path}", flush=True)

    if args.dry_run:
        return 0

    lock_path = Path("/tmp") / f"homekey-reader-{tty_path.name}.lock"
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            f"Another Home Key reader already owns {tty_path}. "
            f"Stop it before starting another."
        )
    lock_file.write(f"{os.getpid()}\n")
    lock_file.flush()

    # The original application expects homekey.json and hap.state relative to
    # its working directory. Run there and replace only its configuration
    # loader with the in-memory copy above.
    os.chdir(reader_project)
    sys.path.insert(0, str(reader_project))

    # PN53x uses a 100 ms acknowledgement read in several places. That is
    # reasonable for a directly attached UART but unnecessarily brittle once
    # the UART is transported over Wi-Fi. Extend only this process's TTY reads;
    # the installed nfcpy package remains unchanged.
    nfc_transport = importlib.import_module("nfc.clf.transport")
    original_tty_read = nfc_transport.TTY.read

    def bridge_tolerant_read(transport, timeout):
        return original_tty_read(
            transport, max(timeout, args.minimum_read_timeout_ms)
        )

    nfc_transport.TTY.read = bridge_tolerant_read

    reader_main = importlib.import_module("main")
    reader_main.load_configuration = lambda _path="configuration.json": configuration
    reader_main.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
