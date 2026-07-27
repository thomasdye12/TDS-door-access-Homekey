from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import ButtonApiConfig


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ButtonResult:
    delivered: bool
    reason: str
    http_status: int | None
    duration_ms: float


class ButtonApiClient:
    def __init__(self, config: ButtonApiConfig) -> None:
        self.config = config

    def send(self, event: dict[str, Any]) -> ButtonResult:
        started = time.monotonic()
        if self.config.url is None:
            return ButtonResult(
                delivered=False,
                reason="button_api_not_configured",
                http_status=None,
                duration_ms=(time.monotonic() - started) * 1000,
            )

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
                body = response.read()
        except urllib.error.HTTPError as error:
            return ButtonResult(
                delivered=False,
                reason=f"button_api_http_{error.code}",
                http_status=int(error.code),
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except (urllib.error.URLError, TimeoutError) as error:
            log.warning("Button API unavailable: %s", error)
            return ButtonResult(
                delivered=False,
                reason="button_api_unavailable",
                http_status=None,
                duration_ms=(time.monotonic() - started) * 1000,
            )

        delivered = 200 <= status < 300
        reason = "button_event_delivered" if delivered else "button_api_failed"
        if body:
            try:
                document = json.loads(body.decode("utf-8"))
                raw_success = document.get(
                    "success", document.get("accepted")
                )
                if isinstance(raw_success, bool):
                    delivered = raw_success
                reason = str(
                    document.get(
                        "reason",
                        (
                            "button_event_delivered"
                            if delivered
                            else "button_event_rejected"
                        ),
                    )
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Doorbell endpoints often return an empty or plain-text 2xx
                # response. HTTP success is sufficient for local LED feedback.
                pass
        return ButtonResult(
            delivered=delivered,
            reason=reason,
            http_status=status,
            duration_ms=(time.monotonic() - started) * 1000,
        )
