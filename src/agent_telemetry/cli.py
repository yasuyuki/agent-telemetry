from __future__ import annotations

import argparse
import json
import math
import sqlite3

from .adapters import load_adapter
from .server import TelemetryHTTPServer
from .storage import Storage


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _positive_finite(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-telemetry")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--db", required=True)
    serve.add_argument("--adapter", default="codex")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_nonnegative, default=4318)
    serve.add_argument("--max-body-bytes", type=_nonnegative, required=True)
    serve.add_argument("--read-timeout", type=_positive_finite, required=True)
    query = commands.add_parser("query")
    query.add_argument("--db", required=True)
    query.add_argument("--session")
    query.add_argument("--since-ns", type=_nonnegative)
    query.add_argument("--until-ns", type=_nonnegative)
    return parser


def run(args: argparse.Namespace) -> int:
    if args.command == "query":
        if args.since_ns is not None and args.until_ns is not None and args.since_ns > args.until_ns:
            raise ValueError("since-ns must not exceed until-ns")
        print(json.dumps(Storage(args.db, readonly=True).query(session=args.session, since_ns=args.since_ns, until_ns=args.until_ns), separators=(",", ":")))
        return 0
    adapter = load_adapter(args.adapter)
    storage = Storage(args.db)
    storage.initialize()
    server = TelemetryHTTPServer((args.host, args.port), adapter, storage, args.max_body_bytes, args.read_timeout)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        return run(parser.parse_args(argv))
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
        return 2
