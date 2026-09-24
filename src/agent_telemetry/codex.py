"""Known Codex event names used by read-only query consumers."""

EVENTS = frozenset({
    "codex.conversation_starts", "codex.api_request", "codex.sse_event",
    "codex.websocket_request", "codex.websocket_event", "codex.user_prompt",
    "codex.tool_decision", "codex.tool_result",
})
