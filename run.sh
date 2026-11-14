#!/usr/bin/env bash
# Start the Lunar Landing Site Navigator.
#   ./run.sh                 -> http://127.0.0.1:8000
#   ./run.sh --port 9000 --host 0.0.0.0
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ./bin/python ]; then
  echo "No virtualenv found in this folder."
  echo "  python3 -m venv . && ./bin/pip install -r requirements.txt"
  exit 1
fi

exec ./bin/python server.py "$@"
