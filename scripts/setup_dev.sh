#!/usr/bin/env bash
# Create a local venv and install dev + runtime deps (mirrors typical CI/local workflow).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pip install requests
echo "Done. Activate with: source .venv/bin/activate"
