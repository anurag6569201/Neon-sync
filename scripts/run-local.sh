#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_BIN="${ROOT_DIR}/.venv/bin"

export PATH="${VENV_BIN}:/opt/homebrew/bin:/usr/local/bin:${PATH}"
export NEON_SYNC_EGRESS_PROXY_URL="${NEON_SYNC_EGRESS_PROXY_URL:-http://127.0.0.1:3128}"
export NEON_SYNC_JOB_DB_PATH="${NEON_SYNC_JOB_DB_PATH:-${ROOT_DIR}/data/jobs.sqlite3}"
export NEON_SYNC_AUTH_DB_PATH="${NEON_SYNC_AUTH_DB_PATH:-${ROOT_DIR}/data/auth.sqlite3}"
export NEON_SYNC_AUTOMATIONS_DB_PATH="${NEON_SYNC_AUTOMATIONS_DB_PATH:-${ROOT_DIR}/data/automations.sqlite3}"
export NEON_SYNC_SCRATCH_DIR="${NEON_SYNC_SCRATCH_DIR:-${ROOT_DIR}/scratch/jobs}"
export NEON_SYNC_STORAGE_DIR="${NEON_SYNC_STORAGE_DIR:-${ROOT_DIR}/data/artifacts}"

echo "Starting Safe Egress Proxy on 127.0.0.1:3128..."
"${VENV_BIN}/neon-sync-proxy" --host 127.0.0.1 --port 3128 &
PROXY_PID=$!

cleanup() {
  echo ""
  echo "Stopping Safe Egress Proxy (PID ${PROXY_PID})..."
  kill "${PROXY_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 0.5
echo "Starting Neon Sync API + Web UI on http://127.0.0.1:8000 ..."
echo "  Web UI : http://127.0.0.1:8000/"
echo "  OpenAPI: http://127.0.0.1:8000/docs"
exec "${VENV_BIN}/neon-sync-api"
