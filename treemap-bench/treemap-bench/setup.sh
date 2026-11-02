#!/usr/bin/env bash
# One-time setup for the treemap benchmark harness.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
echo ">> creating venv with $PY"
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo ">> installing python deps"
pip install --upgrade pip
pip install -r requirements.txt

echo ">> installing playwright chromium"
python -m playwright install chromium

echo
echo "Setup done. Next:"
echo "  source .venv/bin/activate"
echo "  python -m harness.run_bench --smoke        # quick validation (~3 min)"
echo "  python -m harness.run_bench               # full matrix"
