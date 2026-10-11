from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from cosmic.ops.probe import local_ready


class _Ready(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:
        self.send_response(self.status if self.path == "/readyz" else 404)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return None


def test_local_ready() -> None:
    server = HTTPServer(("127.0.0.1", 0), _Ready)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert local_ready(port) is True
        _Ready.status = 503
        assert local_ready(port) is False
    finally:
        server.shutdown()
    assert local_ready(port) is False  # nothing listening
    assert local_ready(0) is False and local_ready(70000) is False
