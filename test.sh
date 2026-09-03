#!/usr/bin/env bash
# Runs the tests. They pass with no network at all: the upstream is faked with an
# httpx MockTransport, so FX_UPSTREAM_BASE can point anywhere (even a closed port).
set -euo pipefail
cd "$(dirname "$0")"

if [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

# Default to a closed port so it is obvious the tests never touch the network.
export FX_UPSTREAM_BASE="${FX_UPSTREAM_BASE:-http://127.0.0.1:9}"
exec python -m pytest -q
