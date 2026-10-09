#!/usr/bin/env bash
# ContextOS launcher for macOS and Linux.
#   ./run.sh              chat app with the models in .env
#   ./run.sh --offline    chat app with simulated replies (no keys needed)
#   ./run.sh test         run the test suite
#   ./run.sh check        make one real call to every configured model
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then PY="python3"
else echo "Python 3.9+ not found. Run install.sh first." >&2; exit 1
fi

# Update to the latest release (5 s timeout, silent when offline). .env, chats and
# keys are never touched. Skip with CONTEXTOS_NO_UPDATE=1 or --no-update.
if [ "${1:-}" = "--no-update" ]; then shift
elif [ "${CONTEXTOS_NO_UPDATE:-}" != "1" ] && [ "${1:-}" != "test" ] && [ "${1:-}" != "check" ]; then
  rc=0; "$PY" -m contextos.update || rc=$?
  if [ "$rc" -eq 10 ]; then
    echo "Restarting on the new version..."
    exec ./run.sh --no-update "$@"
  fi
fi

case "${1:-}" in
  test)  exec "$PY" tests/test_contextos.py ;;
  check) exec "$PY" -m contextos.live --check ;;
  *)     echo "Starting ContextOS at http://127.0.0.1:8000  (Ctrl+C to stop)"
         exec "$PY" -m contextos.server "$@" ;;
esac
