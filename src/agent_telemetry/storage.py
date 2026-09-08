from __future__ import annotations

import sqlite3
from pathlib import Path

from .model import Record


_VERSION = 1
_COLUMNS = ("schema_version", "platform", "event_name", "occurred_at_ns", "received_at_ns", "session_id", "model", "cli_version", "tool_name", "success", "duration_ms", "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens", "reasoning_output_tokens", "total_tokens")
_CREATE = """CREATE TABLE records (
 id INTEGER PRIMARY KEY,
 schema_version INTEGER NOT NULL, platform TEXT NOT NULL, event_name TEXT NOT NULL,
 occurred_at_ns INTEGER, received_at_ns INTEGER NOT NULL, session_id TEXT, model TEXT,
 cli_version TEXT, tool_name TEXT, success INTEGER, duration_ms REAL,
 input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER, cache_write_tokens INTEGER,
 reasoning_output_tokens INTEGER, total_tokens INTEGER
)"""


class Storage:
    def __init__(self, path: str | Path, *, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def initialize(self) -> None:
        if self.readonly:
            raise ValueError("cannot initialize read-only storage")
        con = self._connect()
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='records'").fetchone()
            if version == 0 and table is None:
                if con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                    raise ValueError("incompatible database schema")
                con.execute("BEGIN")
                con.execute(_CREATE)
                con.execute("CREATE INDEX records_session_time ON records(session_id, occurred_at_ns)")
                con.execute("CREATE INDEX records_time ON records(occurred_at_ns)")
                con.execute(f"PRAGMA user_version={_VERSION}")
                con.commit()
            else:
                self._validate_schema(con)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _validate_schema(con: sqlite3.Connection) -> None:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='records'").fetchone()
        if version != _VERSION or table is None:
            raise ValueError("incompatible database schema")
        actual = tuple(row["name"] for row in con.execute("PRAGMA table_info(records)").fetchall())
        if actual != ("id",) + _COLUMNS:
            raise ValueError("incompatible database schema")

    def save(self, records: list[Record]) -> None:
        if type(records) is not list:
            raise ValueError("records must be a list")
        for record in records:
            if type(record) is not Record:
                raise ValueError("records must contain Record instances")
            record.__post_init__()
        if not records:
            return
        con = self._connect()
        try:
            self._validate_schema(con)
            con.execute("BEGIN")
            con.executemany(
                f"INSERT INTO records ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
                [self._values(record) for record in records],
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _values(record: Record) -> tuple[object, ...]:
        return tuple(1 if value is True else 0 if value is False else value for value in (getattr(record, name) for name in _COLUMNS))

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
