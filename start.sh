#!/bin/sh
set -eu
/app/douyin-scanner &
SCANNER_PID=$!
cleanup() {
  kill "$SCANNER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
for i in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:18081/health >/dev/null; then break; fi
  sleep 0.5
done
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}
