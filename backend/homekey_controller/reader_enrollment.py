from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

from homekey_bridge.server import ReaderRecord


log = logging.getLogger(__name__)


def _write_private_json(path: Path, document: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except PermissionError:
        log.warning(
            "Filesystem does not support chmod for reader registry %s",
            path,
        )
    temporary.replace(path)


def enroll_fleet_reader(
    path: Path,
    door_id: str,
    reader_id: str,
    supplied_token: str,
) -> ReaderRecord | None:
    """Authenticate and persist an unknown reader without altering settings."""
    with path.open("r", encoding="utf-8") as source:
        document = json.load(source)
    if not bool(document.get("auto_enroll", True)):
        return None

    fleet_token = str(document.get("fleet_token", "")).strip()
    if not fleet_token:
        log.warning(
            "Cannot auto-enroll reader %s: fleet_token is absent",
            reader_id,
        )
        return None
    expected_token = hmac.new(
        fleet_token.encode("utf-8"),
        reader_id.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_token, expected_token):
        return None

    readers = document.setdefault("readers", {})
    maximum = int(document.get("max_readers", 10))
    if len(readers) >= maximum:
        log.warning(
            "Cannot auto-enroll reader %s: limit of %d reached",
            reader_id,
            maximum,
        )
        return None

    readers[reader_id] = {
        "door_id": door_id,
        "enabled": True,
    }
    _write_private_json(path, document)
    return ReaderRecord(
        reader_id=reader_id,
        door_id=door_id,
        token=expected_token,
        enabled=True,
    )
