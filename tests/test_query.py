from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_telemetry.model import Record
from agent_telemetry.storage import Storage, _COLUMNS


class QueryTests(unittest.TestCase):
    def _fixture(self, path: Path) -> None:
        with sqlite3.connect(path) as con:
            definitions = ", ".join(f"{name} " + (
                "INTEGER" if name in ("schema_version", "occurred_at_ns", "received_at_ns", "success") or name.endswith("_tokens")
                else "REAL" if name == "duration_ms" else "TEXT"
            ) for name in _COLUMNS)
            con.execute(f"CREATE TABLE records (id INTEGER PRIMARY KEY, {definitions})")
            con.execute("PRAGMA user_version=1")
            cols = ", ".join(_COLUMNS)
            marks = ", ".join("?" for _ in _COLUMNS)
            for event, occurred, success in (("codex.user_prompt", 9, None), ("codex.tool_result", 3, 0), ("codex.tool_result", 3, 1)):
                values = {"schema_version": 1, "platform": "codex", "event_name": event, "occurred_at_ns": occurred,
                          "received_at_ns": 10, "session_id": "s", "success": success,
                          "input_tokens": 0 if success == 0 else None}
                con.execute(f"INSERT INTO records ({cols}) VALUES ({marks})", tuple(values.get(c) for c in _COLUMNS))

    def test_existing_v1_query_filters_order_null_and_bool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.sqlite3"
            self._fixture(path)
            rows = Storage(path).query(session="s", since_ns=3, until_ns=9)
            self.assertEqual([r["occurred_at_ns"] for r in rows], [3, 3])
            self.assertEqual([r["success"] for r in rows], [False, True])
            self.assertEqual([r["input_tokens"] for r in rows], [0, None])
            self.assertIsNone(Storage(path).query()[2]["success"])
            self.assertEqual(Record(**rows[0]).event_name, "codex.tool_result")

    def test_missing_or_mismatched_database_is_never_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.sqlite3"
            with self.assertRaises(FileNotFoundError):
                Storage(path).query()
            self.assertFalse(path.exists())
            self._fixture(path)
            with sqlite3.connect(path) as con:
                con.execute("PRAGMA user_version=2")
            with self.assertRaisesRegex(ValueError, "incompatible database schema"):
                Storage(path).query()


if __name__ == "__main__":
    unittest.main()
