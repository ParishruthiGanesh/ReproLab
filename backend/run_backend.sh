#!/usr/bin/env bash
# Start the ReproLab backend (API + background worker).
set -euo pipefail
cd "$(dirname "$0")"
HOST="${REPROLAB_HOST:-127.0.0.1}"
PORT="${REPROLAB_PORT:-8000}"
exec .venv/bin/uvicorn reprolab.main:app --host "$HOST" --port "$PORT" "$@"
