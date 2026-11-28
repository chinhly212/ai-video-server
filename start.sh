#!/bin/sh
set -eu

/usr/local/bin/douyin-scanner &
SCANNER_PID=$!
APP_PID=""

cleanup() {
  if [ -n "$APP_PID" ]; then
    kill "$APP_PID" 2>/dev/null || true
  fi
  kill "$SCANNER_PID" 2>/dev/null || true
  if [ -n "$APP_PID" ]; then
    wait "$APP_PID" 2>/dev/null || true
  fi
  wait "$SCANNER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

SCANNER_READY=0
for i in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:18081/health >/dev/null; then
    SCANNER_READY=1
    break
  fi
  if ! kill -0 "$SCANNER_PID" 2>/dev/null; then
    echo "Douyin scanner process exited before becoming healthy" >&2
    exit 1
  fi
  sleep 0.5
done

if [ "$SCANNER_READY" -ne 1 ]; then
  echo "Douyin scanner did not become healthy within 20 seconds" >&2
  exit 1
fi

uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}" &
APP_PID=$!
wait "$APP_PID"
