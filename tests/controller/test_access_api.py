from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from homekey_controller.access_api import AccessApiClient
from homekey_controller.config import AccessApiConfig


class Handler(BaseHTTPRequestHandler):
    received: dict | None = None
    idempotency_key: str | None = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        Handler.received = json.loads(self.rfile.read(length))
        Handler.idempotency_key = self.headers.get("Idempotency-Key")
        body = json.dumps(
            {
                "success": True,
                "reason": "api_allowed",
                "unlock_ms": 5000,
                "user_id": "resolved-user",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


class AccessApiTests(unittest.TestCase):
    def test_posts_event_and_accepts_unlocked_response(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = AccessApiClient(
                AccessApiConfig(
                    url=f"http://127.0.0.1:{server.server_port}/access",
                    bearer_token="secret",
                    timeout_seconds=1,
                    unavailable_decision="deny",
                )
            )
            decision = client.decide(
                {"event_id": "event-123", "user_id": "user-123"}
            )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertTrue(decision.granted)
        self.assertEqual(decision.reason, "api_allowed")
        self.assertEqual(decision.unlock_ms, 5000)
        self.assertEqual(decision.user_id, "resolved-user")
        self.assertEqual(Handler.idempotency_key, "event-123")
        self.assertEqual(Handler.received["user_id"], "user-123")

    def test_missing_api_fails_closed(self) -> None:
        client = AccessApiClient(
            AccessApiConfig(
                url=None,
                bearer_token=None,
                timeout_seconds=0.1,
                unavailable_decision="deny",
            )
        )
        decision = client.decide({"event_id": "event"})
        self.assertFalse(decision.granted)
        self.assertEqual(decision.reason, "access_api_not_configured")


if __name__ == "__main__":
    unittest.main()
