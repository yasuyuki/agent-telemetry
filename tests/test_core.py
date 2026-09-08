from __future__ import annotations

import http.client
from contextlib import closing
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from agent_telemetry.model import Record
from agent_telemetry import adapters
from agent_telemetry.server import TelemetryHTTPServer, loopback_host
from agent_telemetry.storage import Storage


class Adapter:
    identifier = "test"

    def decode(self, payload: bytes, received_at_ns: int) -> list[Record]:
        if payload == b"bad":
            raise ValueError("bad input")
        return [Record(platform="test", event_name="event", received_at_ns=received_at_ns, occurred_at_ns=4)]


class BrokenStorage:
    def save(self, records: list[Record]) -> None:
        raise RuntimeError("private failure")


class EntryPoints:
    def __init__(self, points: list[object]):
        self.points = points

    def select(self, *, group: str) -> list[object]:
        return self.points if group == "agent_telemetry.adapters" else []


class EntryPoint:
    def __init__(self, name: str, factory: object):
        self.name = name
        self.factory = factory
        self.loaded = False

    def load(self) -> object:
        self.loaded = True
        if isinstance(self.factory, Exception):
            raise self.factory
        return self.factory


class CoreTests(unittest.TestCase):
    def record(self, **values: object) -> Record:
        defaults: dict[str, object] = {"platform": "codex", "event_name": "turn", "received_at_ns": 10, "occurred_at_ns": 5}
        defaults.update(values)
        return Record(**defaults)  # type: ignore[arg-type]

    def test_record_validation(self) -> None:
        self.assertEqual(self.record().platform, "codex")
        with self.assertRaises(ValueError):
            self.record(platform="an email@example.com")
        with self.assertRaises(ValueError):
            self.record(received_at_ns=True)
        with self.assertRaises(ValueError):
            self.record(duration_ms=float("nan"))

    def test_storage_reopen_query_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.sqlite"
            storage = Storage(path)
            storage.initialize()
            storage.save([self.record(session_id="s", occurred_at_ns=9), self.record(session_id="s", occurred_at_ns=3, model=None)])
            reopened = Storage(path, readonly=True)
            self.assertEqual([r["occurred_at_ns"] for r in reopened.query(session="s", since_ns=3, until_ns=9)], [3])
            self.assertIsNone(reopened.query(session="s", since_ns=3)[0]["model"])
            self.assertIsNone(reopened.query(session="s", since_ns=3)[0]["success"])
            with self.assertRaises(FileNotFoundError):
                Storage(Path(tmp) / "missing.sqlite", readonly=True).query()

    def test_save_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "telemetry.sqlite")
            storage.initialize()
            with self.assertRaises(ValueError):
                storage.save([self.record(), object()])  # type: ignore[list-item]
            self.assertEqual(storage.query(), [])

    def test_existing_foreign_database_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.sqlite"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE other (value TEXT)")
            with self.assertRaises(ValueError):
                Storage(path).initialize()
            with closing(sqlite3.connect(path)) as connection, connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT name FROM sqlite_master").fetchall(), [("other",)])

    def test_sql_failure_rolls_back_earlier_inserts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.sqlite"
            storage = Storage(path)
            storage.initialize()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON records
                    WHEN NEW.event_name = 'reject' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                storage.save([self.record(), self.record(event_name="reject")])
            self.assertEqual(Storage(path, readonly=True).query(), [])

    def test_codex_content_never_enters_database(self) -> None:
        from test_codex import payload
        from agent_telemetry.codex import CodexAdapter
        secret = "private-sensitive-command-output-error@example.com"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.sqlite"
            storage = Storage(path)
            storage.initialize()
            storage.save(CodexAdapter().decode(payload({
                key: {"stringValue": secret} for key in
                ("prompt", "arguments", "output", "error", "user.email")
            }, body={"stringValue": secret}), 1))
            self.assertEqual(len(Storage(path, readonly=True).query()), 1)
            self.assertNotIn(secret.encode(), path.read_bytes())

    def _serve(self, storage: object) -> tuple[TelemetryHTTPServer, threading.Thread]:
        server = TelemetryHTTPServer(("127.0.0.1", 0), Adapter(), storage, 64, 1.0)  # type: ignore[arg-type]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _post(self, server: TelemetryHTTPServer, body: bytes, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        merged = {"Content-Type": "application/json", "Content-Encoding": "identity"}
        if headers:
            merged.update(headers)
        connection.request("POST", "/v1/logs", body, headers=merged)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_http_persists_concurrent_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "telemetry.sqlite")
            storage.initialize()
            server, thread = self._serve(storage)
            try:
                results: list[int] = []
                workers = [threading.Thread(target=lambda: results.append(self._post(server, b"{}")[0])) for _ in range(8)]
                for worker in workers: worker.start()
                for worker in workers: worker.join()
                self.assertEqual(results, [200] * 8)
                self.assertEqual(len(storage.query()), 8)
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_http_rejections_and_save_failure(self) -> None:
        server, thread = self._serve(BrokenStorage())
        try:
            status, body = self._post(server, b"{}")
            self.assertEqual((status, body), (503, b""))
            status, _ = self._post(server, b"bad")
            self.assertEqual(status, 400)
            status, _ = self._post(server, b"x" * 65)
            self.assertEqual(status, 413)
            status, _ = self._post(server, b"{}", {"Content-Type": "text/plain"})
            self.assertEqual(status, 415)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_loopback_restriction(self) -> None:
        self.assertTrue(loopback_host("127.0.0.1"))
        self.assertTrue(loopback_host("::1"))
        self.assertFalse(loopback_host("localhost"))
        self.assertFalse(loopback_host("127.evil"))
        self.assertFalse(loopback_host("0.0.0.0"))

    def test_invalid_http_framing_is_rejected_and_closed(self) -> None:
        server, thread = self._serve(BrokenStorage())
        try:
            for headers in (
                b"Content-Length: 2\r\nContent-Length: 2\r\n",
                b"Content-Length: +2\r\n",
                b"Content-Length: 2\r\nTransfer-Encoding: identity\r\n",
                b"Content-Length: 2\r\nContent-Encoding: gzip\r\n",
            ):
                with self.subTest(headers=headers), socket.create_connection(server.server_address, timeout=2) as connection:
                    connection.sendall(b"POST /v1/logs HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n" + headers + b"\r\n{}")
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    self.assertIn(response.status, (400, 415))
                    self.assertEqual(response.getheader("Connection"), "close")
                    response.read()
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_schema_failure_is_service_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.sqlite"
            storage = Storage(path)
            storage.initialize()
            server, thread = self._serve(storage)
            try:
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute("PRAGMA user_version=2")
                self.assertEqual(self._post(server, b"{}"), (503, b""))
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_selected_adapter_does_not_import_unrelated_plugin(self) -> None:
        selected = EntryPoint("test", Adapter)
        broken = EntryPoint("broken", RuntimeError("must not load"))
        with patch.object(adapters, "entry_points", return_value=EntryPoints([selected, broken])):
            self.assertIsInstance(adapters.load_adapter("test"), Adapter)
        self.assertTrue(selected.loaded)
        self.assertFalse(broken.loaded)

    def test_selected_adapter_rejects_duplicate_and_mismatch(self) -> None:
        duplicate = [EntryPoint("test", Adapter), EntryPoint("test", Adapter)]
        with patch.object(adapters, "entry_points", return_value=EntryPoints(duplicate)):
            with self.assertRaises(ValueError):
                adapters.load_adapter("test")
        mismatch = type("Mismatch", (), {"identifier": "other", "decode": lambda self, payload, received: []})
        with patch.object(adapters, "entry_points", return_value=EntryPoints([EntryPoint("good", mismatch)])):
            with self.assertRaises(ValueError):
                adapters.load_adapter("good")

    def test_server_rejects_nonfinite_timeout_and_can_bind_ipv6(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "telemetry.sqlite")
            storage.initialize()
            with self.assertRaises(ValueError):
                TelemetryHTTPServer(("127.0.0.1", 0), Adapter(), storage, 1, float("nan"))
            try:
                server = TelemetryHTTPServer(("::1", 0), Adapter(), storage, 1, 1.0)
            except OSError:
                self.skipTest("IPv6 loopback is unavailable")
            else:
                self.assertEqual(server.address_family, __import__("socket").AF_INET6)
                server.server_close()
