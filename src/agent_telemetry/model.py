from __future__ import annotations

from dataclasses import dataclass
import math
import re


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/+-]+$")
_MAX_SQLITE_INT = (1 << 63) - 1
_STRING_FIELDS = ("platform", "event_name", "session_id", "model", "cli_version", "tool_name")
_INTEGER_FIELDS = (
    "occurred_at_ns", "received_at_ns", "input_tokens", "output_tokens",
    "cached_input_tokens", "cache_write_tokens", "reasoning_output_tokens", "total_tokens",
)


@dataclass(frozen=True, slots=True)
class Record:
    schema_version: int = 1
    platform: str = ""
    event_name: str = ""
    occurred_at_ns: int | None = None
    received_at_ns: int = 0
    session_id: str | None = None
    model: str | None = None
    cli_version: str | None = None
    tool_name: str | None = None
    success: bool | None = None
    duration_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        for name in _STRING_FIELDS:
            value = getattr(self, name)
            if value is None and name not in ("platform", "event_name"):
                continue
            if not isinstance(value, str) or not value or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} must be a non-empty identifier")
        for name in _INTEGER_FIELDS:
            value = getattr(self, name)
            if value is None and name != "received_at_ns":
                continue
            if type(value) is not int or value < 0 or value > _MAX_SQLITE_INT:
                raise ValueError(f"{name} must be a non-negative signed 64-bit integer")
        if self.success is not None and type(self.success) is not bool:
            raise ValueError("success must be boolean or null")
        if self.duration_ms is not None:
            if type(self.duration_ms) not in (int, float) or not math.isfinite(self.duration_ms) or self.duration_ms < 0:
                raise ValueError("duration_ms must be finite and non-negative")
