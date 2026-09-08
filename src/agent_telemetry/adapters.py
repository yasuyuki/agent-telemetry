from __future__ import annotations

from importlib.metadata import entry_points
from typing import Protocol

from .model import Record


class TelemetryAdapter(Protocol):
    identifier: str

    def decode(self, payload: bytes, received_at_ns: int) -> list[Record]: ...


def load_adapter(identifier: str) -> TelemetryAdapter:
    """Load one named adapter without importing unrelated plugin code."""
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("adapter identifier is required")
    points = entry_points()
    selected = points.select(group="agent_telemetry.adapters") if hasattr(points, "select") else points.get("agent_telemetry.adapters", ())
    matches = [point for point in selected if point.name == identifier]
    if not matches:
        raise ValueError(f"unknown adapter: {identifier}")
    if len(matches) != 1:
        raise ValueError(f"duplicate adapter identifier {identifier!r}")
    point = matches[0]
    loaded = point.load()
    adapter = loaded() if isinstance(loaded, type) else loaded
    actual_identifier = getattr(adapter, "identifier", None)
    if actual_identifier != identifier:
        raise ValueError(f"adapter entry point {identifier!r} does not match its identifier")
    if not callable(getattr(adapter, "decode", None)):
        raise ValueError(f"adapter {identifier!r} has no decode method")
    return adapter
