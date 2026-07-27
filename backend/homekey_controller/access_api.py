from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import AccessApiConfig


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccessDecision:
    granted: bool
    reason: str
    unlock_ms: int
    user_id: str | None
    http_status: int | None
    duration_ms: float


class AccessApiClient:
    def __init__(self, config: AccessApiConfig) -> None:
        self.config = config

    def _unavailable(
        self, reason: str, started: float
    ) -> AccessDecision:
        granted = self.config.unavailable_decision == "allow"
        return AccessDecision(
            granted=granted,
            reason=reason,
            unlock_ms=0,
            user_id=None,
            http_status=None,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def decide(self, event: dict[str, Any]) -> AccessDecision:
        started = time.monotonic()
        if self.config.url is None:
            return self._unavailable("access_api_not_configured", started)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": str(event["event_id"]),
            "User-Agent": "homekey-controller/0.1",
        }
        if self.config.bearer_token:
            headers["Authorization"] = (
                f"Bearer {self.config.bearer_token}"
            )
        request = urllib.request.Request(
            self.config.url,
            data=json.dumps(event, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                status = int(response.status)
                document = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return AccessDecision(
                granted=False,
                reason=f"access_api_http_{error.code}",
                unlock_ms=0,
                user_id=None,
                http_status=int(error.code),
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as error:
            log.warning("Access API unavailable: %s", error)
            return self._unavailable("access_api_unavailable", started)

        raw_granted = document.get(
            "granted", document.get("unlocked", document.get("success"))
        )
        if not isinstance(raw_granted, bool):
            return AccessDecision(
                granted=False,
                reason="access_api_invalid_response",
                unlock_ms=0,
                user_id=None,
                http_status=status,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        return AccessDecision(
            granted=raw_granted,
            reason=str(
                document.get(
                    "reason", "access_granted" if raw_granted else "access_denied"
                )
            ),
            unlock_ms=max(0, int(document.get("unlock_ms", 0))),
            user_id=(
                str(document["user_id"])
                if document.get("user_id") is not None
                else None
            ),
            http_status=status,
            duration_ms=(time.monotonic() - started) * 1000,
        )
