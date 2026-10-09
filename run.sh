#!/usr/bin/env bash
# Start VM Manager (API + web UI) on http://$HOST:$PORT
#   HOST=0.0.0.0 ./run.sh   to listen on all interfaces (login with Linux accounts, docs/auth.md; plain http)
#   ./run.sh --dev          backend with auto-reload; run `cd frontend && npm start` for the UI on :3000
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

if [ ! -x backend/venv/bin/python ]; then
  echo "First run: use scripts/setup.sh (installs dependencies and configures libvirt)." >&2
  echo "Then run.sh, or the vm-manager service setup.sh installs." >&2
  exit 1
fi

if [ "${1:-}" != "--dev" ] && [ ! -f frontend/build/index.html ]; then
  (cd frontend && npm ci --legacy-peer-deps && npm run build)
fi

if ! id -nG | grep -qw libvirt; then
  echo "warning: $(id -un) is not in the 'libvirt' group in this session; libvirt will ask polkit for a password." >&2
  echo "         run 'sudo usermod -aG libvirt $(id -un)' and log in again." >&2
fi

cd backend
exec venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --timeout-graceful-shutdown 3 ${1:+--reload}
