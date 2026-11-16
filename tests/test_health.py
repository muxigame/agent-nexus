import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_nexus.supervisor import probe_http


class Unauthorized(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b'{"error": "invalid_token"}')

    def log_message(self, *args):
        pass


def test_4xx_from_an_mcp_endpoint_counts_as_healthy():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Unauthorized)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = probe_http("127.0.0.1", server.server_address[1], "/mcp")
    finally:
        server.shutdown()
        server.server_close()
    assert (result["ok"], result["code"], result["error"]) == (True, 401, "")


def test_closed_port_is_unhealthy():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = probe_http("127.0.0.1", port, "/", timeout=1)
    assert result["ok"] is False and result["code"] is None and result["error"]
