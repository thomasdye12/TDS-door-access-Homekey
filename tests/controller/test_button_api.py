from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from homekey_controller.button_api import ButtonApiClient
from homekey_controller.config import ButtonApiConfig


class Handler(BaseHTTPRequestHandler):
    received: dict | None = None
    idempotency_key: str | None = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        Handler.received = json.loads(self.rfile.read(length))
        Handler.idempotency_key = self.headers.get("Idempotency-Key")
        body = json.dumps(
            {"success": True, "reason": "bell_sent"}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


class ButtonApiTests(unittest.TestCase):
    def test_posts_button_event_and_parses_success(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ButtonApiClient(
                ButtonApiConfig(
                    url=(
                        f"http://127.0.0.1:"
                        f"{server.server_port}/doorbell"
                    ),
                    bearer_token="secret",
                    timeout_seconds=1,
                )
            )
            result = client.send(
                {
                    "event_id": "button-123",
                    "event": "doorbell_button",
                    "reader_id": "c8c9a33859af",
                }
            )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertTrue(result.delivered)
        self.assertEqual(result.reason, "bell_sent")
        self.assertEqual(Handler.idempotency_key, "button-123")
        self.assertEqual(
            Handler.received["reader_id"], "c8c9a33859af"
        )

    def test_missing_button_api_reports_not_configured(self) -> None:
        client = ButtonApiClient(
            ButtonApiConfig(
                url=None,
                bearer_token=None,
                timeout_seconds=0.1,
            )
        )
        result = client.send({"event_id": "button"})
        self.assertFalse(result.delivered)
        self.assertEqual(result.reason, "button_api_not_configured")


if __name__ == "__main__":
    unittest.main()
