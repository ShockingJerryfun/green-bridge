#!/bin/bash
set -e
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec "${PYTHON:-python3}" "$SKILL_DIR/dashboard/dashboard.py" \
    --config "$SKILL_DIR/local/blue.json" "$@"
