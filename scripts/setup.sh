#!/usr/bin/env bash
# Set up the development environment (respects pyproject requires-python; safe venv upgrade)
set -euo pipefail

cd "$(dirname "$0")/.."

env_name=${1:-".venv"}

if [ -d "$env_name" ]; then
  echo "Virtual environment '$env_name' already exists."
else
  echo "Creating Virtual environment..."
  uv venv "$env_name"
fi
echo "Activating virtual environment..."
source "$env_name/bin/activate"

echo "Installing development dependencies..."
python -m pip install --upgrade pip
python -m pip install --upgrade uv
uv pip install -e "."
uv pip install -e ".[test]"
[[ -f requirements_all.txt ]] && uv pip install -r requirements_all.txt

command -v pre-commit &>/dev/null && pre-commit install

echo "✅ Done. Interpreter: $(python -V)."
echo "To activate the venv: source $env_name/bin/activate"
