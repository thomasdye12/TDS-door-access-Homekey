#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from homekey_bridge.protocol import normalize_reader_id
from homekey_controller.config import load_config


ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "firmware" / "esp8266-pn532-websocket"
VERSION_HEADER = FIRMWARE / "include" / "bridge_config.h"
BINARY = FIRMWARE / ".pio" / "build" / "websocket" / "firmware.bin"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build, publish, restart and roll out the current ESP8266 firmware"
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "backend" / "config" / "controller.json",
    )
    parser.add_argument(
        "--target",
        action="append",
        help="Reader MAC to update; repeat as needed (default: all readers)",
    )
    parser.add_argument(
        "--service",
        default="tds-door-access.service",
        help="systemd controller service name",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for readers to report the new version",
    )
    return parser.parse_args()


def firmware_version() -> str:
    source = VERSION_HEADER.read_text(encoding="utf-8")
    match = re.search(
        r'FIRMWARE_VERSION\[\]\s*=\s*"([A-Za-z0-9._-]+)"',
        source,
    )
    if match is None:
        raise SystemExit(f"Could not read FIRMWARE_VERSION from {VERSION_HEADER}")
    return match.group(1)


def platformio() -> str:
    bundled = FIRMWARE / ".venv" / "bin" / "pio"
    if bundled.is_file():
        return str(bundled)
    executable = shutil.which("pio")
    if executable is None:
        raise SystemExit(
            "PlatformIO is unavailable; install it or create the firmware .venv"
        )
    return executable


def request_json(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    document: dict | None = None,
) -> object:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if document is not None:
        body = json.dumps(document).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def wait_for_controller(base_url: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            health = request_json(f"{base_url}/health")
            if isinstance(health, dict) and health.get("status") == "ok":
                return
        except (HTTPError, URLError, TimeoutError, ValueError):
            pass
        time.sleep(1)
    raise SystemExit("Controller did not become healthy after restart")


def wait_for_readers(
    base_url: str,
    token: str,
    targets: list[str],
    version: str,
    deadline: float,
) -> None:
    while time.monotonic() < deadline:
        try:
            status = request_json(
                f"{base_url}/api/readers",
                token=token,
            )
            if isinstance(status, list):
                selected = (
                    status
                    if targets == ["*"]
                    else [
                        reader
                        for reader in status
                        if reader.get("reader_id") in targets
                    ]
                )
                if selected and all(
                    reader.get("connected")
                    and reader.get("firmware") == version
                    for reader in selected
                ):
                    return
        except (HTTPError, URLError, TimeoutError, ValueError):
            pass
        time.sleep(2)
    raise SystemExit(
        f"Timed out waiting for target readers to report firmware {version}; "
        "check journalctl -u tds-door-access.service"
    )


def wait_for_connections(
    base_url: str,
    token: str,
    targets: list[str],
    deadline: float,
) -> None:
    while time.monotonic() < deadline:
        try:
            status = request_json(
                f"{base_url}/api/readers",
                token=token,
            )
            if isinstance(status, list):
                selected = (
                    status
                    if targets == ["*"]
                    else [
                        reader
                        for reader in status
                        if reader.get("reader_id") in targets
                    ]
                )
                if selected and all(
                    reader.get("connected") for reader in selected
                ):
                    return
        except (HTTPError, URLError, TimeoutError, ValueError):
            pass
        time.sleep(1)
    raise SystemExit(
        "Target readers did not reconnect after the controller restart"
    )


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    token = config.firmware_server.admin_token
    if not token:
        raise SystemExit(
            "firmware_server.admin_token is required in controller.json"
        )
    targets = (
        ["*"]
        if not args.target or "*" in args.target
        else [normalize_reader_id(value) for value in args.target]
    )
    version = firmware_version()
    print(f"[1/6] Building firmware {version}")
    subprocess.run(
        [
            platformio(),
            "run",
            "-d",
            str(FIRMWARE),
            "-e",
            "websocket",
        ],
        check=True,
    )

    print(f"[2/6] Publishing firmware {version}")
    publish = [
        sys.executable,
        str(ROOT / "backend" / "manage_controller.py"),
        "--config",
        str(args.config),
        "publish-firmware",
        "--binary",
        str(BINARY),
        "--version",
        version,
    ]
    for target in targets:
        if target != "*":
            publish.extend(["--target", target])
    subprocess.run(
        publish,
        check=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
    )

    print(f"[3/6] Restarting {args.service}")
    restart = ["systemctl"] if os.geteuid() == 0 else ["sudo", "systemctl"]
    subprocess.run([*restart, "restart", args.service], check=True)

    base_url = f"http://127.0.0.1:{config.firmware_server.port}"
    deadline = time.monotonic() + max(args.timeout, 30)
    wait_for_controller(base_url, deadline)
    print("[4/6] Waiting for target readers to reconnect")
    wait_for_connections(base_url, token, targets, deadline)

    print(f"[5/6] Triggering rollout to {', '.join(targets)}")
    result = request_json(
        f"{base_url}/api/firmware/rollout",
        token=token,
        method="POST",
        document={"targets": targets},
    )
    if not isinstance(result, dict) or not result.get("accepted"):
        raise SystemExit(f"Controller rejected firmware rollout: {result!r}")

    print(f"[6/6] Waiting for readers to report firmware {version}")
    wait_for_readers(base_url, token, targets, version, deadline)
    print(f"Deployment complete: firmware {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
