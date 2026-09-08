import dataclasses
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_telemetry.claude import ClaudeAdapter
from agent_telemetry.server import TelemetryHTTPServer
from agent_telemetry.storage import Storage


def payload(attrs=None, resource_attrs=None, **log):
    attributes = {"event.name": {"stringValue": "tool_result"}}
    attributes.update(attrs or {})
    if resource_attrs is None:
        resource_attrs = {"app.version": {"stringValue": "1.2.3"}}
    return json.dumps({"resourceLogs": [{
        "resource": {"attributes": [{"key": key, "value": value} for key, value in resource_attrs.items()]},
        "scopeLogs": [{"logRecords": [{
            "attributes": [{"key": key, "value": value} for key, value in attributes.items()], **log,
        }]}],
    }]}).encode()


class ClaudeTests(unittest.TestCase):
    def test_projection_event_normalization_and_types(self):
        for event, expected in (
            ("user_prompt", "claude_code.user_prompt"),
            ("api_request", "claude_code.api_request"),
            ("api_error", "claude_code.api_error"),
            ("tool_decision", "claude_code.tool_decision"),
            ("claude_code.tool_result", "claude_code.tool_result"),
        ):
            with self.subTest(event=event):
                row, = ClaudeAdapter().decode(payload({
                    "event.name": {"stringValue": event},
                    "session.id": {"stringValue": "session-123"},
                    "model": {"stringValue": "claude-test"},
                    "tool_name": {"stringValue": "Bash"},
                    "success": {"boolValue": True},
                    "duration_ms": {"doubleValue": 12.5},
                    "input_tokens": {"intValue": "42"},
                    "output_tokens": {"stringValue": "8"},
                    "cache_read_tokens": {"intValue": "4"},
                    "cache_creation_tokens": {"intValue": "2"},
                }, timeUnixNano="1234567890"), 9876543210)
                self.assertEqual((row.platform, row.event_name, row.session_id, row.model, row.cli_version),
                                 ("claude", expected, "session-123", "claude-test", "1.2.3"))
                self.assertEqual((row.tool_name, row.success, row.duration_ms), ("Bash", True, 12.5))
                self.assertEqual((row.input_tokens, row.output_tokens, row.cached_input_tokens,
                                  row.cache_write_tokens), (42, 8, 4, 2))
                self.assertIsNone(row.total_tokens)
                self.assertIsNone(row.reasoning_output_tokens)
                self.assertEqual((row.occurred_at_ns, row.received_at_ns), (1234567890, 9876543210))

    def test_cli_version_prefers_app_version_and_only_falls_back_for_cli_resource(self):
        row, = ClaudeAdapter().decode(payload({"app.version": {"stringValue": "4.5.6"}}, {
            "service.name": {"stringValue": "claude-code"},
            "service.version": {"stringValue": "9.9.9"},
        }), 1)
        self.assertEqual(row.cli_version, "4.5.6")
        row, = ClaudeAdapter().decode(payload(resource_attrs={
            "service.name": {"stringValue": "claude-code"},
            "service.version": {"stringValue": "9.9.9"},
        }), 1)
        self.assertEqual(row.cli_version, "9.9.9")
        row, = ClaudeAdapter().decode(payload(resource_attrs={
            "service.name": {"stringValue": "claude-code-desktop"},
            "service.version": {"stringValue": "9.9.9"},
        }), 1)
        self.assertIsNone(row.cli_version)

    def test_missing_zero_and_timestamp_precision(self):
        row, = ClaudeAdapter().decode(payload(timeUnixNano="0"), 1)
        for key in ("session_id", "model", "success", "duration_ms", "input_tokens", "output_tokens",
                    "cached_input_tokens", "cache_write_tokens", "total_tokens", "reasoning_output_tokens"):
            self.assertIsNone(getattr(row, key))
        row, = ClaudeAdapter().decode(payload({
            "input_tokens": {"intValue": "0"},
            "output_tokens": {"intValue": "0"},
            "cache_read_tokens": {"intValue": "0"},
            "cache_creation_tokens": {"intValue": "0"},
        }), 1)
        self.assertEqual((row.input_tokens, row.output_tokens, row.cached_input_tokens, row.cache_write_tokens),
                         (0, 0, 0, 0))
        row, = ClaudeAdapter().decode(payload({
            "success": {"stringValue": "false"},
            "duration_ms": {"doubleValue": 0},
        }), 1)
        self.assertEqual((row.success, row.duration_ms), (False, 0))
        for timestamp in ("1970-01-01T00:00:01.123456789Z", "1970-01-01T01:00:01.123456789+01:00"):
            row, = ClaudeAdapter().decode(payload({"event.timestamp": {"stringValue": timestamp}}), 7)
            self.assertEqual(row.occurred_at_ns, 1123456789)
        row, = ClaudeAdapter().decode(payload({"event.timestamp": {"stringValue": "1970-01-01T00:00:01Z"}}, timeUnixNano="9"), 7)
        self.assertEqual(row.occurred_at_ns, 9)

    def test_error_and_decision_do_not_infer_success(self):
        for event in ("api_error", "tool_decision"):
            row, = ClaudeAdapter().decode(payload({"event.name": {"stringValue": event}}), 1)
            self.assertIsNone(row.success)

    def test_never_retains_body_or_unallowlisted_values(self):
        secret = "private prompt command output failure person@example.com"
        row, = ClaudeAdapter().decode(payload({
            key: {"stringValue": secret} for key in
            ("prompt", "arguments", "output", "error", "user.email", "tool_name", "model", "session.id")
        }, body={"stringValue": secret}), 1)
        self.assertNotIn(secret, json.dumps(dataclasses.asdict(row)))
        self.assertIsNone(row.tool_name)
        self.assertIsNone(row.model)
        self.assertIsNone(row.session_id)
        self.assertEqual(ClaudeAdapter().decode(payload({"event.name": {"stringValue": "claude_code.future"}}, body={"stringValue": secret}), 1), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.sqlite"
            storage = Storage(path)
            storage.initialize()
            storage.save([row])
            self.assertNotIn(secret.encode(), path.read_bytes())

    def test_rejects_malformed_payloads(self):
        for data in (b"not json", b"[]", b'{"resourceLogs":null}', b'{"resourceLogs":[null]}',
                     b'{"resourceLogs":[],"resourceLogs":[]}', b'{"x":NaN}', b'\xff'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ClaudeAdapter().decode(data, 1)
        for key, value in (("success", {"intValue": "1"}),
                           ("duration_ms", {"doubleValue": 10**400}),
                           ("input_tokens", {"intValue": "-1"}),
                           ("input_tokens", {"boolValue": True}),
                           ("duration_ms", {"stringValue": "nan"}),
                           ("duration_ms", {"stringValue": "-2"}),
                           ("output_tokens", {"intValue": str(2**63)})):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                ClaudeAdapter().decode(payload({key: value}), 1)
        with self.assertRaises(ValueError):
            ClaudeAdapter().decode(payload({"event.timestamp": {"stringValue": "1970-01-01T00:00:01"}}), 1)

    def test_http_malformed_tail_does_not_persist_any_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "telemetry.sqlite")
            storage.initialize()
            server = TelemetryHTTPServer(("127.0.0.1", 0), ClaudeAdapter(), storage, 65536, 1.0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                data = json.loads(payload())
                data["resourceLogs"][0]["scopeLogs"][0]["logRecords"].append(None)
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("POST", "/v1/logs", json.dumps(data).encode(), {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                connection.close()
                self.assertEqual(storage.query(), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
