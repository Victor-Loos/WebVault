#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -m ruff check .
"$PYTHON" -m ruff format --check .
"$PYTHON" -m pyright
"$PYTHON" -m pytest -q
sh -n garage/init.sh setup.sh update.sh orchestrator/entrypoint.sh
bash -n garage/resize.sh
docker compose config --quiet
git diff --check
