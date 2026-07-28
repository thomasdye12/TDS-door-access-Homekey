from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import deploy_firmware


class DeployFirmwareTests(unittest.TestCase):
    def test_degraded_health_still_means_controller_is_ready(self) -> None:
        with patch.object(
            deploy_firmware,
            "request_json",
            return_value={
                "status": "failure",
                "controller_id": "homekey-main",
                "failed_readers": [{"reader_id": "308398b5feb3"}],
            },
        ):
            deploy_firmware.wait_for_controller(
                "http://127.0.0.1:8766",
                time.monotonic() + 1,
            )


if __name__ == "__main__":
    unittest.main()
