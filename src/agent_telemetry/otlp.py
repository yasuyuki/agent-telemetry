"""Shared OTLP JSON structure and scalar validation; no event policy."""

import json
import math
import re
from datetime import datetime, timezone

IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/+\-]+\Z")


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
    if kind == "doubleValue" and type(scalar) in (int, float) and _finite(scalar):
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


def logs(payload):
    try:
        data = _object(json.loads(
            payload, object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
        ))
        for resource in _array(data.get("resourceLogs", [])):
            resource = _object(resource)
            metadata = _attributes(_object(resource.get("resource", {})).get("attributes", []))
            for scope in _array(resource.get("scopeLogs", [])):
                scope = _object(scope)
                for log in _array(scope.get("logRecords", [])):
                    log = _object(log)
                    yield metadata, log, _attributes(log.get("attributes", []))
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("invalid JSON") from exc


def _duration(value):
    if value is not None:
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise ValueError("invalid duration") from None
        if type(value) not in (int, float) or not _finite(value) or value < 0:
            raise ValueError("invalid duration")
    return value


def _success(value):
    if value in ("true", "false"):
        value = value == "true"
    if value is not None and type(value) is not bool:
        raise ValueError("invalid success")
    return value


def _finite(value):
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
