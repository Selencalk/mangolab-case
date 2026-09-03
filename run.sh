#!/usr/bin/env bash
# Starts the service. Listens on $PORT (default 8080) and reads the upstream
# base URL from $FX_UPSTREAM_BASE (default https://api.frankfurter.dev) — both
# are read by the app, so pointing FX_UPSTREAM_BASE at a fake upstream just works.
set -euo pipefail
cd "$(dirname "$0")"

# Use the project venv if present.
if [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

export PORT="${PORT:-8080}"
exec python -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
