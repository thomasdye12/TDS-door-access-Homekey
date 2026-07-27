from __future__ import annotations

import os
import socket
import threading
import time
import unittest

from tools.pty_tcp_bridge import PtyTcpBridge


class PtyTcpBridgeTests(unittest.TestCase):
    def test_bidirectional_forwarding(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()

        bridge = PtyTcpBridge(host, port, reconnect_delay=0.01)
        worker = threading.Thread(target=bridge.run, daemon=True)
        worker.start()

        listener.settimeout(2)
        peer, _ = listener.accept()
        tty_fd = os.open(bridge.tty_path, os.O_RDWR | os.O_NOCTTY)
        try:
            os.write(tty_fd, b"host-to-pn532")
            peer.settimeout(2)
            self.assertEqual(peer.recv(64), b"host-to-pn532")

            peer.sendall(b"pn532-to-host")
            deadline = time.monotonic() + 2
            received = b""
            while not received and time.monotonic() < deadline:
                received = os.read(tty_fd, 64)
            self.assertEqual(received, b"pn532-to-host")
            self.assertTrue(bridge.nfcpy_path.endswith(":pn532"))
        finally:
            bridge.stop()
            peer.close()
            listener.close()
            os.close(tty_fd)
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

