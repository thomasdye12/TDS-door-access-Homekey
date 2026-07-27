from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path
    encryption_key_file: Path


@dataclass(frozen=True)
class HomeKitConfig:
    enabled: bool
    port: int
    persist_file: Path
    display_name: str


@dataclass(frozen=True)
class AccessApiConfig:
    url: str | None
    bearer_token: str | None
    timeout_seconds: float
    unavailable_decision: str


@dataclass(frozen=True)
class ButtonApiConfig:
    url: str | None
    bearer_token: str | None
    timeout_seconds: float


@dataclass(frozen=True)
class FirmwareServerConfig:
    enabled: bool
    host: str
    port: int
    manifest_path: Path
    admin_token: str | None


@dataclass(frozen=True)
class ControllerConfig:
    controller_id: str
    door_id: str
    database: DatabaseConfig
    homekit: HomeKitConfig
    access_api: AccessApiConfig
    button_api: ButtonApiConfig
    firmware_server: FirmwareServerConfig
    reader_registry: Path
    websocket_host: str
    websocket_port: int
    throttle_polling: float


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: Path) -> ControllerConfig:
    path = path.expanduser().resolve()
    with path.open("r", encoding="utf-8") as source:
        raw: dict[str, Any] = json.load(source)
    base = path.parent
    database = raw.get("database", {})
    homekit = raw.get("homekit", {})
    access_api = raw.get("access_api", {})
    button_api = raw.get("button_api", {})
    firmware_server = raw.get("firmware_server", {})
    websocket = raw.get("websocket", {})

    unavailable_decision = str(
        access_api.get("unavailable_decision", "deny")
    ).lower()
    if unavailable_decision not in {"allow", "deny"}:
        raise ValueError(
            "access_api.unavailable_decision must be 'allow' or 'deny'"
        )

    return ControllerConfig(
        controller_id=str(raw.get("controller_id", "homekey-controller")),
        door_id=str(raw.get("door_id", "logical-door")),
        database=DatabaseConfig(
            path=_resolve(base, database.get("path", "controller.sqlite3")),
            encryption_key_file=_resolve(
                base, database.get("encryption_key_file", "controller.key")
            ),
        ),
        homekit=HomeKitConfig(
            enabled=bool(homekit.get("enabled", True)),
            port=int(homekit.get("port", 51926)),
            persist_file=_resolve(
                base, homekit.get("persist_file", "homekit.state")
            ),
            display_name=str(
                homekit.get("display_name", "Home Key Registration")
            ),
        ),
        access_api=AccessApiConfig(
            url=(
                str(access_api["url"]).strip()
                if access_api.get("url")
                else None
            ),
            bearer_token=(
                str(access_api["bearer_token"])
                if access_api.get("bearer_token")
                else None
            ),
            timeout_seconds=float(access_api.get("timeout_seconds", 0.75)),
            unavailable_decision=unavailable_decision,
        ),
        button_api=ButtonApiConfig(
            url=(
                str(button_api["url"]).strip()
                if button_api.get("url")
                else None
            ),
            bearer_token=(
                str(button_api["bearer_token"])
                if button_api.get("bearer_token")
                else None
            ),
            timeout_seconds=float(
                button_api.get("timeout_seconds", 1.5)
            ),
        ),
        firmware_server=FirmwareServerConfig(
            enabled=bool(firmware_server.get("enabled", True)),
            host=str(firmware_server.get("host", "0.0.0.0")),
            port=int(firmware_server.get("port", 8766)),
            manifest_path=_resolve(
                base,
                firmware_server.get(
                    "manifest_path", "firmware/manifest.json"
                ),
            ),
            admin_token=(
                str(firmware_server["admin_token"])
                if firmware_server.get("admin_token")
                else None
            ),
        ),
        reader_registry=_resolve(
            base, raw.get("reader_registry", "readers.json")
        ),
        websocket_host=str(websocket.get("host", "0.0.0.0")),
        websocket_port=int(websocket.get("port", 8765)),
        throttle_polling=float(raw.get("throttle_polling", 0.1)),
    )
