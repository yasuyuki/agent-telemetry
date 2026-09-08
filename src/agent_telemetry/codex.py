"""Allowlisted projection of Codex OTLP/HTTP JSON logs; never persist bodies."""

from .otlp import logs, _scalar, _integer, _identifier, _timestamp, _duration, _success

from .model import Record


EVENTS = frozenset({
    "codex.conversation_starts", "codex.api_request", "codex.sse_event",
    "codex.websocket_request", "codex.websocket_event", "codex.user_prompt",
    "codex.tool_decision", "codex.tool_result",
})
TOKEN_FIELDS = {
    "input_tokens": "input_token_count",
    "output_tokens": "output_token_count",
    "cached_input_tokens": "cached_token_count",
    "cache_write_tokens": "cache_write_token_count",
    "reasoning_output_tokens": "reasoning_token_count",
    "total_tokens": "total_token_count",
}


class CodexAdapter:
    identifier = "codex"

    def decode(self, payload: bytes, received_at_ns: int) -> list[Record]:
        records = []
        for metadata, log, attrs in logs(payload):
            event = _scalar(attrs, "event.name")
            if event not in EVENTS:
                continue
            combined = metadata | attrs
            occurred = log.get("timeUnixNano")
            if occurred is not None:
                occurred = _integer(occurred) or None
            if occurred is None:
                occurred = _timestamp(_scalar(attrs, "event.timestamp"))
            duration = _duration(_scalar(attrs, "duration_ms"))
            success = _success(_scalar(attrs, "success"))
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
