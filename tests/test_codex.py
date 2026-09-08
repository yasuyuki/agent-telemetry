import dataclasses
import json
import unittest

from agent_telemetry.codex import CodexAdapter


def payload(attrs=None, **log):
    attributes = {"event.name": {"stringValue": "codex.tool_result"}}
    attributes.update(attrs or {})
    return json.dumps({"resourceLogs": [{
        "resource": {"attributes": [{"key": "app.version", "value": {"stringValue": "0.153.4"}}]},
        "scopeLogs": [{"logRecords": [{
            "attributes": [{"key": k, "value": v} for k, v in attributes.items()], **log,
        }]}],
    }]}).encode()


class CodexTests(unittest.TestCase):
    def test_projection_and_types(self):
        row, = CodexAdapter().decode(payload({
            "conversation.id": {"stringValue": "1234-5678"},
            "model": {"stringValue": "gpt-test"},
            "tool_name": {"stringValue": "exec_command"},
            "success": {"boolValue": True},
            "duration_ms": {"doubleValue": 12.5},
            "input_token_count": {"intValue": "42"},
            "output_token_count": {"stringValue": "8"},
            "cached_token_count": {"intValue": "4"},
            "cache_write_token_count": {"intValue": "2"},
            "reasoning_token_count": {"intValue": "3"},
        }, timeUnixNano="1234567890"), 9876543210)
        self.assertEqual((row.platform, row.session_id, row.model, row.cli_version),
                         ("codex", "1234-5678", "gpt-test", "0.153.4"))
        self.assertEqual((row.tool_name, row.success, row.duration_ms), ("exec_command", True, 12.5))
        self.assertEqual((row.input_tokens, row.output_tokens, row.cached_input_tokens,
                          row.cache_write_tokens, row.reasoning_output_tokens), (42, 8, 4, 2, 3))
        self.assertIsNone(row.total_tokens)  # Never sum or invent an upstream total.
        self.assertEqual((row.occurred_at_ns, row.received_at_ns), (1234567890, 9876543210))

    def test_missing_and_zero(self):
        row, = CodexAdapter().decode(payload(timeUnixNano="0"), 1)
        self.assertIsNone(row.occurred_at_ns)
        for key in ("session_id", "model", "success", "duration_ms", "input_tokens"):
            self.assertIsNone(getattr(row, key))
        row, = CodexAdapter().decode(payload({"input_token_count": {"intValue": "0"}}), 1)
        self.assertEqual(row.input_tokens, 0)

    def test_codex_timestamp_fallback_preserves_nanoseconds(self):
        for timestamp in ("1970-01-01T00:00:01.123456789Z", "1970-01-01T01:00:01.123456789+01:00"):
            row, = CodexAdapter().decode(payload({"event.timestamp": {"stringValue": timestamp}}), 7)
            self.assertEqual(row.occurred_at_ns, 1123456789)
        row, = CodexAdapter().decode(payload({"event.timestamp": {"stringValue": "1970-01-01T00:00:01Z"}}, timeUnixNano="9"), 7)
        self.assertEqual(row.occurred_at_ns, 9)
        with self.assertRaises(ValueError):
            CodexAdapter().decode(payload({"event.timestamp": {"stringValue": "1970-01-01T00:00:01"}}), 7)

    def test_never_retains_content(self):
        secret = "private prompt command output failure person@example.com"
        row, = CodexAdapter().decode(payload({
            key: {"stringValue": secret} for key in
            ("prompt", "arguments", "output", "error", "user.email", "tool_name", "model", "conversation.id")
        }, body={"stringValue": secret}), 1)
        self.assertNotIn(secret, json.dumps(dataclasses.asdict(row)))
        self.assertIsNone(row.tool_name)
        self.assertIsNone(row.model)
        self.assertIsNone(row.session_id)

    def test_unknown_events_and_empty_batch(self):
        self.assertEqual(CodexAdapter().decode(payload({"event.name": {"stringValue": "codex.future"}}), 1), [])
        self.assertEqual(CodexAdapter().decode(b"{}", 1), [])

    def test_rejects_invalid_payloads(self):
        for data in (b"not json", b"[]", b'{"resourceLogs":null}',
                     b'{"resourceLogs":[null]}', b'{"resourceLogs":[],"resourceLogs":[]}',
                     b'{"x":NaN}', b'\xff'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                CodexAdapter().decode(data, 1)
        for key, value in (("success", {"intValue": "1"}),
                           ("input_token_count", {"intValue": "-1"}),
                           ("input_token_count", {"boolValue": True}),
                           ("duration_ms", {"stringValue": "nan"}),
                           ("duration_ms", {"stringValue": "-2"}),
                           ("output_token_count", {"intValue": str(2**63)})):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                CodexAdapter().decode(payload({key: value}), 1)

    def test_batch_malformed_tail_is_not_partial_success(self):
        data = json.loads(payload())
        data["resourceLogs"][0]["scopeLogs"][0]["logRecords"].append(None)
        with self.assertRaises(ValueError):
            CodexAdapter().decode(json.dumps(data).encode(), 1)


if __name__ == "__main__":
    unittest.main()
