"""End-to-end checks against the pinned, built Collector binary."""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest

from agent_telemetry.storage import Storage


ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("AGENT_TELEMETRY_COLLECTOR")
SECRET = "synthetic-secret-482a"


def attr(key: str, value: object) -> dict[str, object]:
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    else:
        encoded = {"stringValue": value}
    return {"key": key, "value": encoded}


def payload(platform: str, *, bad: bool = False, unknown: bool = False) -> bytes:
    name = "codex.tool_result" if platform == "codex" else "tool_result"
    identifier = "conversation.id" if platform == "codex" else "session.id"
    attrs = [attr("event.name", "unknown." + SECRET if unknown else name),
             attr(identifier, "session-1"), attr("success", "invalid" if bad else False),
             attr("input_token_count" if platform == "codex" else "input_tokens", 0),
             attr("tool_name", "Bash"), attr("prompt", SECRET), attr("arguments", SECRET),
             attr("output", SECRET), attr("error", SECRET), attr("unknown", SECRET)]
    data = {"resourceLogs": [{"resource": {"attributes": [attr("service.name", "claude-code"),
             attr("service.version", "1.2.3"), attr("user.email", SECRET), attr("unknown", SECRET)]},
             "scopeLogs": [{"scope": {"name": SECRET, "version": SECRET,
             "attributes": [attr("unknown", SECRET)]}, "logRecords": [
             {"timeUnixNano": "12", "body": {"stringValue": SECRET}, "severityText": SECRET,
              "attributes": attrs}]}]}]}
    return json.dumps(data, separators=(",", ":")).encode()


@unittest.skipUnless(BINARY, "set AGENT_TELEMETRY_COLLECTOR to require real binary integration")
class CollectorIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.db = self.dir / "events.sqlite3"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        config = (ROOT / "collector" / "config.yaml").read_text()
        config = config.replace("127.0.0.1:4318", f"127.0.0.1:{self.port}")
        config = config.replace("max_request_body_size: 8388608", "max_request_body_size: 4096")
        config = config.replace("read_timeout: 10s", "read_timeout: 1s")
        self.config = self.dir / "config.yaml"
        self.config.write_text(config)
        self.output = b""

    @contextlib.contextmanager
    def collector(self, platform: str):
        env = os.environ | {"AGENT_TELEMETRY_DB": str(self.db), "AGENT_TELEMETRY_PLATFORM": platform}
        proc = subprocess.Popen([BINARY, f"--config={self.config}"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    _, err = proc.communicate()
                    raise AssertionError("collector exited before listening: " + err.decode(errors="replace"))
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.05)
            else:
                raise AssertionError("collector did not listen")
            yield
        finally:
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate(timeout=5)
            self.output += out + err

    def post(self, data: bytes) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.request("POST", "/v1/logs", data, {"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    def test_commit_ack_atomicity_privacy_and_existing_query(self) -> None:
        with self.collector("codex"):
            valid = json.loads(payload("codex"))
            invalid = json.loads(payload("codex", bad=True))
            valid["resourceLogs"][0]["scopeLogs"][0]["logRecords"].extend(
                invalid["resourceLogs"][0]["scopeLogs"][0]["logRecords"])
            self.assertGreaterEqual(self.post(json.dumps(valid).encode()), 400)
            self.assertEqual(Storage(self.db).query(), [])
            self.assertEqual(self.post(payload("codex", unknown=True)), 200)
            self.assertEqual(Storage(self.db).query(), [])
            self.assertEqual(self.post(payload("codex")), 200)
            rows = Storage(self.db).query(session="session-1", since_ns=12, until_ns=13)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["success"], rows[0]["input_tokens"]), (False, 0))
            self.assertIsNone(rows[0]["total_tokens"])
            self.assertEqual(rows[0]["event_name"], "codex.tool_result")
        # A successful HTTP response remains readable after Collector exits.
        self.assertEqual(len(Storage(self.db).query()), 1)
        with self.collector("claude"):
            self.assertEqual(self.post(payload("claude")), 200)
        rows = Storage(self.db).query(session="session-1")
        self.assertEqual([r["platform"] for r in rows], ["codex", "claude"])
        self.assertEqual(rows[1]["event_name"], "claude_code.tool_result")
        self.assertNotIn(SECRET.encode(), self.db.read_bytes())
        self.assertNotIn(SECRET.encode(), self.output)
        self.assertNotIn(SECRET, json.dumps(rows))
        for path in self.dir.iterdir():
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), path.name)

    def test_receiver_limits_and_schema_failure_do_not_ack(self) -> None:
        with self.collector("codex"):
            self.assertGreaterEqual(self.post(b"x" * 4097), 400)
            malformed = json.loads(payload("codex"))
            malformed_attrs = malformed["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"]
            malformed_attrs[0]["value"] = {"arrayValue": {"values": [{"stringValue": SECRET}]}}
            self.assertEqual(self.post(json.dumps(malformed).encode()), 200)
            self.assertEqual(Storage(self.db).query(), [])
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as slow:
                slow.sendall(b"POST /v1/logs HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
                slow.settimeout(3)
                reply = slow.recv(4096)
                self.assertNotIn(b" 200 ", reply)
            self.assertEqual(self.post(payload("codex")), 200)
            with sqlite3.connect(self.db) as con:
                con.execute("PRAGMA user_version=2")
            self.assertGreaterEqual(self.post(payload("codex")), 400)
            self.assertEqual(len(StorageQueryWithoutVersion(self.db)), 1)
        self.assertNotIn(SECRET.encode(), self.output)

    def test_concurrent_requests_and_lost_response_retry(self) -> None:
        with self.collector("codex"):
            data = payload("codex")
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(self.post, [data] * 4)), [200] * 4)
            self.assertEqual(len(Storage(self.db).query()), 4)
            # Send a complete request but deliberately never read its response.
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as lost:
                headers = (f"POST /v1/logs HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                           f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n")
                lost.sendall(headers.encode() + data)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and len(Storage(self.db).query()) == 4:
                    time.sleep(.02)
                self.assertEqual(len(Storage(self.db).query()), 5)
            self.assertEqual(self.post(data), 200)
            self.assertEqual(len(Storage(self.db).query()), 6)


def StorageQueryWithoutVersion(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as con:
        return con.execute("SELECT id FROM records").fetchall()


if __name__ == "__main__":
    unittest.main()
