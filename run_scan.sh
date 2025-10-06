#!/usr/bin/env bash
set -euo pipefail
source .venv/bin/activate
python scan_drive.py "$@"
