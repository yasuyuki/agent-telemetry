from __future__ import annotations

import argparse
import json
import sqlite3

from .storage import Storage


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-telemetry")
    commands = parser.add_subparsers(dest="command", required=True)
    query = commands.add_parser("query")
    query.add_argument("--db", required=True)
    query.add_argument("--session")
    query.add_argument("--since-ns", type=_nonnegative)
    query.add_argument("--until-ns", type=_nonnegative)
    return parser


def run(args: argparse.Namespace) -> int:
    if args.since_ns is not None and args.until_ns is not None and args.since_ns > args.until_ns:
        raise ValueError("since-ns must not exceed until-ns")
    rows = Storage(args.db, readonly=True).query(
        session=args.session, since_ns=args.since_ns, until_ns=args.until_ns,
    )
    print(json.dumps(rows, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        return run(parser.parse_args(argv))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
        return 2
