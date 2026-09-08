"""Allowlisted projection of Codex OTLP/HTTP JSON logs; never persist bodies."""

import json
import math
import re
from datetime import datetime, timezone

from .model import Record


EVENTS = frozenset({
    "codex.conversation_starts", "codex.api_request", "codex.sse_event",
    "codex.websocket_request", "codex.websocket_event", "codex.user_prompt",
    "codex.tool_decision", "codex.tool_result",
})
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/+\-]+\Z")
TOKEN_FIELDS = {
    "input_tokens": "input_token_count",
    "output_tokens": "output_token_count",
    "cached_input_tokens": "cached_token_count",
    "cache_write_tokens": "cache_write_token_count",
    "reasoning_output_tokens": "reasoning_token_count",
    "total_tokens": "total_token_count",
}


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _array(value):
    if not isinstance(value, list):
        raise ValueError("expected array")
    return value


def _attributes(value):
    result = {}
    for item in _array(value):
        item = _object(item)
        key = item.get("key")
        if not isinstance(key, str) or key in result:
            raise ValueError("invalid attribute key")
        result[key] = _object(item.get("value"))
    return result


def _scalar(attrs, key):
    value = attrs.get(key)
    if value is None:
        return None
    if len(value) != 1:
        raise ValueError("invalid scalar")
    kind, scalar = next(iter(value.items()))
    if kind == "stringValue" and isinstance(scalar, str):
        return scalar
    if kind == "boolValue" and type(scalar) is bool:
        return scalar
    if kind == "intValue":
        return _integer(scalar)
    if kind == "doubleValue" and type(scalar) in (int, float) and math.isfinite(scalar):
        return scalar
    raise ValueError("invalid scalar")


def _integer(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("invalid nonnegative integer")
    return value


def _identifier(value):
    # Drop values containing whitespace, email addresses, or free text.
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid event timestamp")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if match is None:
        raise ValueError("invalid event timestamp")
    base, fraction, offset = match.groups()
    instant = datetime.fromisoformat(base + ("+00:00" if offset == "Z" else offset))
    delta = instant.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return _integer((delta.days * 86400 + delta.seconds) * 10**9 + int((fraction or "").ljust(9, "0")))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class CodexAdapter:
    identifier = "codex"

    def decode(self, payload: bytes, received_at_ns: int) -> list[Record]:
        try:
            data = json.loads(payload, object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            return self._decode(_object(data), received_at_ns)
        except (UnicodeError, RecursionError, OverflowError) as exc:
            raise ValueError("invalid JSON") from exc

    def _decode(self, data, received_at_ns):
        records = []
        for resource in _array(data.get("resourceLogs", [])):
            resource = _object(resource)
            metadata = _attributes(_object(resource.get("resource", {})).get("attributes", []))
            for scope in _array(resource.get("scopeLogs", [])):
                scope = _object(scope)
                for log in _array(scope.get("logRecords", [])):
                    log = _object(log)
                    attrs = _attributes(log.get("attributes", []))
                    event = _scalar(attrs, "event.name")
                    if event not in EVENTS:
                        continue
                    combined = metadata | attrs
                    occurred = log.get("timeUnixNano")
                    if occurred is not None:
                        occurred = _integer(occurred) or None
                    if occurred is None:
                        occurred = _timestamp(_scalar(attrs, "event.timestamp"))
                    duration = _scalar(attrs, "duration_ms")
                    if duration is not None:
                        if isinstance(duration, str):
                            try:
                                duration = float(duration)
                            except ValueError:
                                raise ValueError("invalid duration") from None
                        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                            raise ValueError("invalid duration")
                    success = _scalar(attrs, "success")
                    if success in ("true", "false"):
                        success = success == "true"
                    if success is not None and type(success) is not bool:
                        raise ValueError("invalid success")
                    tokens = {}
                    for target, source in TOKEN_FIELDS.items():
                        value = _scalar(attrs, source)
                        tokens[target] = None if value is None else _integer(value)
                    records.append(Record(
                        platform=self.identifier, event_name=event,
                        occurred_at_ns=occurred, received_at_ns=received_at_ns,
                        session_id=_identifier(_scalar(combined, "conversation.id")),
                        model=_identifier(_scalar(combined, "model")),
                        cli_version=_identifier(_scalar(combined, "app.version")),
                        tool_name=_identifier(_scalar(attrs, "tool_name")),
                        success=success, duration_ms=duration, **tokens,
                    ))
        return records
