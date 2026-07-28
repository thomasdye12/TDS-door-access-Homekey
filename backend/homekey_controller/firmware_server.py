from __future__ import annotations

import base64
import hmac
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from homekey_bridge.protocol import MessageType, normalize_reader_id
from homekey_bridge.server import ReaderManager, ReaderRecord

from .config import ControllerConfig, FirmwareServerConfig


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FirmwareRelease:
    version: str
    binary_path: Path
    size: int
    md5: str
    sha256: str
    targets: tuple[str, ...]

    def targets_reader(self, reader_id: str) -> bool:
        return "*" in self.targets or reader_id in self.targets


class FirmwareRepository:
    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path
        self._lock = threading.Lock()

    def load(self) -> FirmwareRelease | None:
        with self._lock:
            if not self.manifest_path.exists():
                return None
            with self.manifest_path.open("r", encoding="utf-8") as source:
                document = json.load(source)

        binary_name = str(document["binary"])
        if Path(binary_name).name != binary_name:
            raise ValueError("firmware binary must be a plain filename")
        binary_path = self.manifest_path.parent / binary_name
        if (
            binary_path.resolve().parent
            != self.manifest_path.parent.resolve()
        ):
            raise ValueError("firmware binary escapes repository directory")
        size = int(document["size"])
        if not binary_path.is_file() or binary_path.stat().st_size != size:
            raise ValueError("firmware binary is missing or has wrong size")
        targets = tuple(str(value) for value in document.get("targets", ["*"]))
        if not targets:
            raise ValueError("firmware manifest has no rollout targets")
        md5 = str(document["md5"]).lower()
        sha256 = str(document["sha256"]).lower()
        if not re.fullmatch(r"[0-9a-f]{32}", md5):
            raise ValueError("firmware manifest has invalid MD5")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("firmware manifest has invalid SHA-256")
        return FirmwareRelease(
            version=str(document["version"]),
            binary_path=binary_path,
            size=size,
            md5=md5,
            sha256=sha256,
            targets=targets,
        )

    def set_targets(self, targets: list[str]) -> FirmwareRelease:
        with self._lock:
            if not self.manifest_path.exists():
                raise FileNotFoundError("no published firmware manifest")
            with self.manifest_path.open("r", encoding="utf-8") as source:
                document = json.load(source)
            document["targets"] = targets
            temporary = self.manifest_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(document, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.manifest_path)
        release = self.load()
        if release is None:
            raise FileNotFoundError("no published firmware manifest")
        return release


class FirmwareHttpServer:
    def __init__(
        self,
        config: FirmwareServerConfig,
        controller_config: ControllerConfig,
        manager: ReaderManager,
    ) -> None:
        self.config = config
        self.controller_config = controller_config
        self.manager = manager
        self.repository = FirmwareRepository(config.manifest_path)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._notifier = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="firmware-rollout",
        )

    def _reader_from_basic_auth(
        self, authorization: str | None
    ) -> ReaderRecord | None:
        if not authorization or not authorization.startswith("Basic "):
            return None
        try:
            raw = base64.b64decode(
                authorization[6:], validate=True
            ).decode("utf-8")
            reader_value, supplied_token = raw.split(":", 1)
            reader_id = normalize_reader_id(reader_value)
        except (ValueError, UnicodeDecodeError):
            return None
        record = self.manager.registry.get(reader_id)
        if (
            record is None
            or not record.enabled
            or not record.accepts_token(supplied_token)
        ):
            return None
        return record

    def _admin_authorized(self, authorization: str | None) -> bool:
        token = self.config.admin_token
        if token is None or not authorization:
            return False
        prefix = "Bearer "
        return authorization.startswith(prefix) and hmac.compare_digest(
            authorization[len(prefix) :], token
        )

    def _reader_status(self) -> list[dict]:
        return self.manager.reader_status()

    def _notify_targets(self, targets: tuple[str, ...]) -> None:
        reader_ids = (
            tuple(self.manager.registry)
            if "*" in targets
            else targets
        )
        for reader_id in reader_ids:
            connection = self.manager.get(reader_id)
            if connection is None:
                continue
            try:
                connection.request(
                    MessageType.FIRMWARE_UPDATE_CHECK,
                    timeout_ms=500,
                )
            except Exception as error:
                log.warning(
                    "Could not notify reader %s of firmware rollout: %s",
                    reader_id,
                    error,
                )

    def _handler_type(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "TDSDoorAccessFirmware/1"

            def log_message(self, format_string, *args):
                log.debug(
                    "Firmware HTTP %s - %s",
                    self.address_string(),
                    format_string % args,
                )

            def _json(self, status: int, document: dict | list) -> None:
                body = json.dumps(document, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _admin(self) -> bool:
                if service._admin_authorized(
                    self.headers.get("Authorization")
                ):
                    return True
                self._json(401, {"error": "unauthorized"})
                return False

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/health":
                    try:
                        release = service.repository.load()
                        firmware_status = "ready" if release else "empty"
                    except (OSError, ValueError, KeyError, json.JSONDecodeError):
                        release = None
                        firmware_status = "invalid"
                    readers = service._reader_status()
                    enabled_readers = [
                        item for item in readers if item["enabled"]
                    ]
                    all_readers_online = bool(enabled_readers) and all(
                        item["state"] == "online"
                        for item in enabled_readers
                    )
                    failed_readers = [
                        {
                            "reader_id": item["reader_id"],
                            "door_id": item["door_id"],
                            "state": item["state"],
                            "reason": item["reason"],
                        }
                        for item in enabled_readers
                        if item["state"] != "online"
                    ]
                    self._json(
                        200,
                        {
                            "status": (
                                "ok" if all_readers_online else "failure"
                            ),
                            "controller_id": (
                                service.controller_config.controller_id
                            ),
                            "door_id": service.controller_config.door_id,
                            "all_readers_online": all_readers_online,
                            "connected_readers": sum(
                                int(item["connected"]) for item in readers
                            ),
                            "online_readers": sum(
                                int(item["state"] == "online")
                                for item in enabled_readers
                            ),
                            "configured_readers": len(readers),
                            "failed_readers": failed_readers,
                            "readers": readers,
                            "firmware_version": (
                                release.version
                                if release is not None
                                else None
                            ),
                            "firmware_status": firmware_status,
                        },
                    )
                    return
                if path == "/api/readers":
                    if self._admin():
                        self._json(200, service._reader_status())
                    return
                if path == "/api/firmware":
                    if not self._admin():
                        return
                    try:
                        release = service.repository.load()
                    except (
                        OSError,
                        ValueError,
                        KeyError,
                        json.JSONDecodeError,
                    ) as error:
                        self._json(
                            503,
                            {
                                "published": False,
                                "error": str(error),
                            },
                        )
                        return
                    self._json(
                        200,
                        (
                            {"published": False}
                            if release is None
                            else {
                                "published": True,
                                "version": release.version,
                                "size": release.size,
                                "sha256": release.sha256,
                                "targets": list(release.targets),
                            }
                        ),
                    )
                    return
                if path != "/firmware/latest":
                    self._json(404, {"error": "not_found"})
                    return

                record = service._reader_from_basic_auth(
                    self.headers.get("Authorization")
                )
                if record is None:
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate", 'Basic realm="firmware"'
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    release = service.repository.load()
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    json.JSONDecodeError,
                ):
                    self._json(503, {"error": "firmware_repository_invalid"})
                    return
                current_version = self.headers.get(
                    "x-ESP8266-version", ""
                )
                if (
                    release is None
                    or current_version == release.version
                    or not release.targets_reader(record.reader_id)
                ):
                    self.send_response(304)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                self.send_response(200)
                self.send_header(
                    "Content-Type", "application/octet-stream"
                )
                self.send_header("Content-Length", str(release.size))
                self.send_header("x-MD5", release.md5)
                self.send_header(
                    "X-Firmware-Version", release.version
                )
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with release.binary_path.open("rb") as binary:
                    while chunk := binary.read(64 * 1024):
                        self.wfile.write(chunk)

            def do_POST(self):
                path = urlsplit(self.path).path
                if path != "/api/firmware/rollout":
                    self._json(404, {"error": "not_found"})
                    return
                if not self._admin():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16 * 1024:
                        raise ValueError("invalid body length")
                    document = json.loads(self.rfile.read(length))
                    raw_targets = document.get("targets", ["*"])
                    if not isinstance(raw_targets, list) or not raw_targets:
                        raise ValueError("targets must be a non-empty list")
                    if "*" in raw_targets:
                        targets = ["*"]
                    else:
                        targets = [
                            normalize_reader_id(str(value))
                            for value in raw_targets
                        ]
                        unknown = set(targets) - set(service.manager.registry)
                        if unknown:
                            raise ValueError(
                                "unknown readers: "
                                + ", ".join(sorted(unknown))
                            )
                    release = service.repository.set_targets(targets)
                except (
                    FileNotFoundError,
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as error:
                    self._json(400, {"error": str(error)})
                    return

                service._notifier.submit(
                    service._notify_targets, release.targets
                )
                self._json(
                    202,
                    {
                        "accepted": True,
                        "version": release.version,
                        "targets": list(release.targets),
                    },
                )

        return Handler

    def start(self) -> None:
        if not self.config.enabled or self._server is not None:
            return
        self._server = ThreadingHTTPServer(
            (self.config.host, self.config.port),
            self._handler_type(),
        )
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="firmware-http",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "Firmware endpoint listening on http://%s:%d",
            self.config.host,
            self.config.port,
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._notifier.shutdown(wait=True, cancel_futures=True)
        self._server = None
        self._thread = None
