#!/usr/bin/env bash
# Local PostgreSQL for integration tests and the demo profile.
# Uses the pinned `pgserver` distribution (ADR-0010) so no daemon is required.
set -euo pipefail
cd "$(dirname "$0")/.."
BIN="${VIRTUAL_ENV:-.venv}/bin"
DATA_DIR="${AIOPS_PG_DATA:-$(pwd)/.pgdata}"
PORT_FILE="${DATA_DIR}/.port"

case "${1:-start}" in
  start)
    "$BIN/python" - <<'PY'
import os, pathlib, pgserver, urllib.parse
data = pathlib.Path(os.environ.get("AIOPS_PG_DATA", ".pgdata")).resolve()
server = pgserver.get_server(str(data))
uri = server.get_uri()
# pgserver returns a unix-socket URI; expose a TCP URL too for containers.
pathlib.Path(data / ".uri").write_text(uri)
print(uri)
PY
    echo "$(cat "$DATA_DIR/.uri")"
    ;;
  stop)
    "$BIN/python" -c "
import os, pathlib, pgserver
data = pathlib.Path(os.environ.get('AIOPS_PG_DATA', '.pgdata')).resolve()
try:
    pgserver.get_server(str(data)).cleanup()
    print('stopped')
except Exception as exc:
    print('already stopped', exc)
"
    ;;
  url)
    echo "$(cat "$DATA_DIR/.uri")"
    ;;
  *)
    echo "usage: $0 {start|stop|url}" >&2; exit 2 ;;
esac
