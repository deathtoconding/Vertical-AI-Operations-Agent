#!/usr/bin/env python3
"""Run a local PostgreSQL for development, tests and the demo profile.

Uses the pinned ``pgserver`` distribution (ADR-0010): no daemon, no container, no
system packages. Started with ``cleanup_mode=None`` so the server survives this process —
useful when the caller wants a database that outlives a single command.

Usage::

    python scripts/pg_server.py --data .pgdata --database aiops   # runs until Ctrl-C
    python scripts/pg_server.py --print-url
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.persistence.pg import database_url_from_socket  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default=str(REPO_ROOT / ".pgdata"))
    parser.add_argument("--database", default="aiops")
    parser.add_argument("--print-url", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)

    import pgserver

    data_dir = pathlib.Path(args.data).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.stop:
        server = pgserver.get_server(str(data_dir), cleanup_mode=None)
        server.cleanup()
        print("postgres stopped")
        return 0

    server = pgserver.get_server(str(data_dir), cleanup_mode=None)
    uri = server.get_uri()
    (data_dir / ".uri").write_text(uri)
    try:
        server.psql(f'CREATE DATABASE "{args.database}";')
    except Exception as exc:  # psql() has no ignore-errors mode
        # The only expected failure is "database already exists"; anything else surfaces here.
        print(f"note: CREATE DATABASE skipped ({type(exc).__name__}: {exc})", file=sys.stderr)

    url = database_url_from_socket(uri, args.database)
    (data_dir / ".url").write_text(url)
    if args.print_url:
        print(url)
        return 0

    print(json.dumps({"uri": uri, "url": url, "pgdata": str(data_dir)}), flush=True)
    print("postgres ready; ctrl-c to stop this supervisor (server keeps running)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
