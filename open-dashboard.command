#!/usr/bin/env bash
# Double-click this file in Finder to open the local intelligence dashboard.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  printf 'The project environment is missing. Ask Ryan to repair the local setup.\n'
  read -r -p 'Press Enter to close... '
  exit 1
fi

PORT=8765
URL="http://127.0.0.1:${PORT}/"

printf 'Starting Maricopa + Pima Document Intel at %s\n' "$URL"
printf 'Keep this Terminal window open while using the dashboard.\n'
printf 'Close this window or press Control-C to stop the local server.\n\n'

.venv/bin/python -m http.server "$PORT" --bind 127.0.0.1 >.dashboard-server.log 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for _ in {1..50}; do
  if curl -fsS "$URL" >/dev/null 2>&1; then
    open "$URL"
    wait "$SERVER_PID"
    exit $?
  fi
  sleep 0.1
done

printf 'The local dashboard server did not start. See %s/.dashboard-server.log\n' "$PWD"
exit 1
