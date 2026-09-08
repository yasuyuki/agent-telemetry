"""Allowlisted projection of Claude OTLP/HTTP JSON logs; never persist bodies."""

from .otlp import logs, _scalar, _integer, _identifier, _timestamp, _duration, _success

from .model import Record


EVENTS = frozenset({"user_prompt", "api_request", "api_error", "tool_decision", "tool_result"})
TOKEN_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_input_tokens": "cache_read_tokens",
    "cache_write_tokens": "cache_creation_tokens",
}


class ClaudeAdapter:
    identifier = "claude"

    def decode(self, payload: bytes, received_at_ns: int) -> list[Record]:
        records = []
        for metadata, log, attrs in logs(payload):
            event = _scalar(attrs, "event.name")
            if isinstance(event, str):
                event = event.removeprefix("claude_code.")
            if event not in EVENTS:
                continue
            combined = metadata | attrs
            occurred = log.get("timeUnixNano")
            if occurred is not None:
                occurred = _integer(occurred) or None
            if occurred is None:
                occurred = _timestamp(_scalar(attrs, "event.timestamp"))
            version = _scalar(combined, "app.version")
            # Desktop service.version identifies the host app, not the CLI.
            if version is None and _scalar(metadata, "service.name") == "claude-code":
                version = _scalar(metadata, "service.version")
            duration = _duration(_scalar(attrs, "duration_ms"))
            success = _success(_scalar(attrs, "success"))
            tokens = {}
            for target, source in TOKEN_FIELDS.items():
                value = _scalar(attrs, source)
                tokens[target] = None if value is None else _integer(value)
            records.append(Record(
                platform=self.identifier, event_name="claude_code." + event,
                occurred_at_ns=occurred, received_at_ns=received_at_ns,
                session_id=_identifier(_scalar(combined, "session.id")),
                model=_identifier(_scalar(combined, "model")),
                cli_version=_identifier(version),
                tool_name=_identifier(_scalar(attrs, "tool_name")),
                success=success, duration_ms=duration, **tokens,
            ))
        return records
