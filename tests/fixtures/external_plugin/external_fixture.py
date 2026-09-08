from agent_telemetry import Record


class FixtureAdapter:
    identifier = "fixture"

    def decode(self, payload: bytes, received_at_ns: int) -> list[Record]:
        if payload != b'{"fixture":true}':
            raise ValueError("malformed fixture payload")
        return [Record(platform="fixture", event_name="received", received_at_ns=received_at_ns)]
