#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

# Ensure venv is set up
if [ ! -d .venv ]; then
  echo "→ Running setup..."
  scripts/setup.sh
fi

# Start MA with debug logging and custom data dir
# Use separate dir to not mix with production config
DATA_DIR="${MA_DEV_DATA:-$HOME/.musicassistant-dev}"
mkdir -p "$DATA_DIR"

echo "→ Data dir: $DATA_DIR"
echo "→ UI at:    http://localhost:8095"

.venv/bin/python -m music_assistant \
  --data-dir "$DATA_DIR" \
  --cache-dir "$DATA_DIR/.cache" \
  --log-level "${LOG_LEVEL:-debug}"
