from __future__ import annotations

import sqlite3
from pathlib import Path


_VERSION = 1
_COLUMNS = (
    "schema_version", "platform", "event_name", "occurred_at_ns", "received_at_ns",
    "session_id", "model", "cli_version", "tool_name", "success", "duration_ms",
    "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens",
    "reasoning_output_tokens", "total_tokens",
)


class Storage:
    """Read an existing schema-v1 store without creating or modifying it."""

    def __init__(self, path: str | Path, *, readonly: bool = True):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _validate_schema(con: sqlite3.Connection) -> None:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='records'").fetchone()
        if version != _VERSION or table is None:
            raise ValueError("incompatible database schema")
        actual = tuple(row["name"] for row in con.execute("PRAGMA table_info(records)").fetchall())
        if actual != ("id",) + _COLUMNS:
            raise ValueError("incompatible database schema")

    def query(self, *, session: str | None = None, since_ns: int | None = None, until_ns: int | None = None) -> list[dict[str, object]]:
        con = self._connect()
        try:
            self._validate_schema(con)
            clauses: list[str] = []
            values: list[object] = []
            if session is not None:
                clauses.append("session_id = ?")
                values.append(session)
            if since_ns is not None:
                clauses.append("occurred_at_ns >= ?")
                values.append(since_ns)
            if until_ns is not None:
                clauses.append("occurred_at_ns < ?")
                values.append(until_ns)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = con.execute("SELECT id, " + ", ".join(_COLUMNS) + " FROM records" + where + " ORDER BY occurred_at_ns, id", values)
            output = []
            for row in rows:
                item = {key: row[key] for key in row.keys() if key != "id"}
                if item["success"] is not None:
                    item["success"] = bool(item["success"])
                output.append(item)
            return output
        finally:
            con.close()
