#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

from homekey_bridge.protocol import normalize_reader_id
from homekey_bridge.server import load_registry
from homekey_controller.config import load_config
from homekey_controller.store import SQLiteKeyStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Manage the standalone Home Key controller"
    )
    root.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config" / "controller.json",
    )
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("status")

    events = commands.add_parser("events")
    events.add_argument("--limit", type=int, default=50)

    migrate = commands.add_parser("import-legacy")
    migrate.add_argument("--homekey-json", type=Path, required=True)
    migrate.add_argument("--hap-state", type=Path)
    migrate.add_argument("--force", action="store_true")

    mapping = commands.add_parser("map-endpoint")
    mapping.add_argument("endpoint_id")
    mapping.add_argument("user_id")

    card_mapping = commands.add_parser("map-card")
    card_mapping.add_argument("card_uid")
    card_mapping.add_argument("user_id")

    add_reader = commands.add_parser("add-reader")
    add_reader.add_argument("reader_id")
    add_reader.add_argument("--door-id")

    reader_token = commands.add_parser("reader-token")
    reader_token.add_argument("reader_id")

    publish = commands.add_parser("publish-firmware")
    publish.add_argument("--binary", type=Path, required=True)
    publish.add_argument("--version", required=True)
    publish.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="Reader MAC to target; repeat as needed (default: all)",
    )

    commands.add_parser("generate-admin-token")

    return root


def _load_reader_registry(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def _write_private_json(path: Path, document: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except PermissionError:
        print(
            f"Warning: filesystem does not support chmod for {path}; "
            "enforce access with server-side permissions",
            file=sys.stderr,
        )
    temporary.replace(path)


def _derived_reader_token(document: dict, reader_id: str) -> str:
    fleet_token = str(document.get("fleet_token", "")).strip()
    if not fleet_token:
        raise SystemExit(
            "Reader registry has no fleet_token; configure fleet mode first"
        )
    return hmac.new(
        fleet_token.encode("utf-8"),
        reader_id.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def main() -> int:
    args = parser().parse_args()
    config = load_config(args.config)

    if args.command == "generate-admin-token":
        with args.config.expanduser().resolve().open(
            "r", encoding="utf-8"
        ) as source:
            document = json.load(source)
        token = secrets.token_hex(32)
        document.setdefault("firmware_server", {})["admin_token"] = token
        _write_private_json(
            args.config.expanduser().resolve(),
            document,
        )
        print(token)
        return 0

    if args.command == "publish-firmware":
        version = str(args.version).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
            raise SystemExit("Invalid firmware version")
        source = args.binary.expanduser().resolve()
        if not source.is_file() or source.stat().st_size == 0:
            raise SystemExit(f"Firmware binary does not exist: {source}")
        payload = source.read_bytes()
        if not payload or payload[0] != 0xE9:
            raise SystemExit("File is not an ESP8266 application binary")
        if version.encode("ascii") not in payload:
            raise SystemExit(
                "Firmware binary does not contain the declared version"
            )
        targets = (
            ["*"]
            if not args.targets or "*" in args.targets
            else [normalize_reader_id(value) for value in args.targets]
        )
        unknown = set(targets) - set(load_registry(config.reader_registry))
        if "*" not in targets and unknown:
            raise SystemExit(
                "Unknown readers: " + ", ".join(sorted(unknown))
            )
        destination_directory = (
            config.firmware_server.manifest_path.parent
        )
        destination_directory.mkdir(parents=True, exist_ok=True)
        destination = destination_directory / f"firmware-{version}.bin"
        if destination.exists() and destination.read_bytes() != payload:
            raise SystemExit(
                f"Firmware version {version} is already published with "
                "different bytes; increment FIRMWARE_VERSION and rebuild"
            )
        if not destination.exists():
            temporary_binary = destination.with_suffix(".bin.tmp")
            shutil.copy2(source, temporary_binary)
            temporary_binary.replace(destination)
        manifest = {
            "version": version,
            "binary": destination.name,
            "size": len(payload),
            "md5": hashlib.md5(payload).hexdigest(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "targets": targets,
        }
        temporary_manifest = (
            config.firmware_server.manifest_path.with_suffix(".json.tmp")
        )
        temporary_manifest.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_manifest.replace(
            config.firmware_server.manifest_path
        )
        print(json.dumps(manifest, indent=2))
        return 0

    if args.command in {"add-reader", "reader-token"}:
        reader_id = normalize_reader_id(args.reader_id)
        document = _load_reader_registry(config.reader_registry)
        token = _derived_reader_token(document, reader_id)
        if args.command == "reader-token":
            print(token)
            return 0
        readers = document.setdefault("readers", {})
        if reader_id in readers:
            raise SystemExit(f"Reader {reader_id} already exists")
        readers[reader_id] = {
            "door_id": args.door_id or config.door_id,
            "enabled": True,
        }
        _write_private_json(config.reader_registry, document)
        print(
            f"Added reader {reader_id}; restart the controller to enable it"
        )
        print(
            "OTA hostname: "
            f"TDS-Door-Access-V2-{reader_id}.local"
        )
        return 0

    store = SQLiteKeyStore(
        config.database.path,
        config.database.encryption_key_file,
    )

    if args.command == "status":
        print(json.dumps(store.summary(), indent=2))
    elif args.command == "events":
        print(json.dumps(store.list_events(args.limit), indent=2))
    elif args.command == "map-endpoint":
        store.map_endpoint(args.endpoint_id, args.user_id)
        print(
            f"Mapped endpoint {args.endpoint_id.lower()} to user {args.user_id}"
        )
    elif args.command == "map-card":
        store.map_credential("rfid_uid", args.card_uid, args.user_id)
        print(
            f"Mapped RFID UID {args.card_uid.upper()} to user {args.user_id}"
        )
    elif args.command == "import-legacy":
        result = store.import_legacy_document(
            args.homekey_json, force=args.force
        )
        if args.hap_state is not None:
            destination = config.homekit.persist_file
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not args.force:
                raise SystemExit(
                    f"HomeKit state already exists: {destination}"
                )
            shutil.copy2(args.hap_state.expanduser().resolve(), destination)
        print(
            "Imported "
            f"{result['issuers']} issuers and {result['endpoints']} endpoints"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
