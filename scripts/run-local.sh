#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_BIN="${ROOT_DIR}/.venv/bin"
cd "${ROOT_DIR}"

if [[ ! -x "${VENV_BIN}/neon-sync-api" ]]; then
  echo 'Install first: python3 -m venv .venv && .venv/bin/pip install -e ".[engines,webpage]"'
  exit 1
fi

export PATH="${VENV_BIN}:/opt/homebrew/bin:/usr/local/bin:${PATH}"
export NEON_SYNC_EGRESS_PROXY_URL="${NEON_SYNC_EGRESS_PROXY_URL:-http://127.0.0.1:3128}"
export NEON_SYNC_JOB_DB_PATH="${NEON_SYNC_JOB_DB_PATH:-${ROOT_DIR}/data/jobs.sqlite3}"
export NEON_SYNC_SCRATCH_DIR="${NEON_SYNC_SCRATCH_DIR:-${ROOT_DIR}/scratch/jobs}"
export NEON_SYNC_STORAGE_DIR="${NEON_SYNC_STORAGE_DIR:-${ROOT_DIR}/data/artifacts}"

PROXY_PID=""
API_PID=""

cleanup() {
  if [[ -n "${API_PID}" ]]; then kill "${API_PID}" 2>/dev/null || true; fi
  if [[ -n "${PROXY_PID}" ]]; then kill "${PROXY_PID}" 2>/dev/null || true; fi
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "${NEON_SYNC_EGRESS_PROXY_URL}" == "http://127.0.0.1:3128" ]]; then
  echo "Starting Safe Egress Proxy on 127.0.0.1:3128..."
  "${VENV_BIN}/neon-sync-proxy" --host 127.0.0.1 --port 3128 &
  PROXY_PID=$!
  "${VENV_BIN}/python" - <<'PY'
import socket
import time
for attempt in range(50):
    try:
        with socket.create_connection(("127.0.0.1", 3128), timeout=0.2):
            break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("The egress proxy did not start. Check whether port 3128 is available.")
PY
  kill -0 "${PROXY_PID}"
fi

echo "Starting Neon Sync. The UI is available on the configured API host and port (default http://127.0.0.1:8000/)."
"${VENV_BIN}/neon-sync-api" &
API_PID=$!
wait "${API_PID}"
