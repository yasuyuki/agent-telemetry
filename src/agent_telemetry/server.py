from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import math
import re
import socket
import time
from typing import Any

from .adapters import TelemetryAdapter
from .storage import Storage


def loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class TelemetryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], adapter: TelemetryAdapter, storage: Storage, max_body_bytes: int, read_timeout: float):
        if not loopback_host(address[0]):
            raise ValueError("host must be a loopback literal")
        if max_body_bytes < 1 or not math.isfinite(read_timeout) or read_timeout <= 0:
            raise ValueError("limits must be positive")
        self.adapter = adapter
        self.storage = storage
        self.max_body_bytes = max_body_bytes
        self.read_timeout = read_timeout
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, TelemetryHandler)

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, client_address = super().get_request()
        request.settimeout(self.read_timeout)
        return request, client_address

    def handle_error(self, request: object, client_address: object) -> None:
        # Request and adapter failures must not reach stderr, where payload-derived
        # exception text might otherwise be collected by a service manager.
        pass


class TelemetryHandler(BaseHTTPRequestHandler):
    server: TelemetryHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != "/v1/logs":
            self._reply(404)
            return
        if self.headers.get_all("Transfer-Encoding"):
            self._reply(400)
            return
        encodings = self.headers.get_all("Content-Encoding") or []
        if len(encodings) > 1 or (encodings and encodings[0].lower() != "identity"):
            self._reply(415)
            return
        types = self.headers.get_all("Content-Type") or []
        if len(types) != 1 or types[0] != "application/json":
            self._reply(415)
            return
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1:
            self._reply(400)
            return
        if re.fullmatch(r"[0-9]+", lengths[0]) is None:
            self._reply(400)
            return
        try:
            length = int(lengths[0])
        except ValueError:
            self._reply(400)
            return
        if length > self.server.max_body_bytes:
            self._reply(413)
            return
        self.connection.settimeout(self.server.read_timeout)
        try:
            payload = self.rfile.read(length)
        except (OSError, socket.timeout):
            self._reply(400)
            return
        if len(payload) != length:
            self._reply(400)
            return
        try:
            records = self.server.adapter.decode(payload, time.time_ns())
            if not isinstance(records, list):
                raise ValueError("adapter result")
        except ValueError:
            self._reply(400)
            return
        except Exception:
            self._reply(503)
            return
        try:
            self.server.storage.save(records)
        except Exception:
            self._reply(503)
            return
        self._reply(200, b"{}")

    def do_GET(self) -> None:
        self._reply(404)

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
