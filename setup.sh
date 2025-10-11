#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv || true
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
[ -f requirements.txt ] && pip install -r requirements.txt || echo "No requirements.txt"
echo "✔ setup.sh terminé."
