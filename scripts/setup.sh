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

# Install pre-commit hooks if pre-commit is available
if command -v pre-commit &>/dev/null; then
  pre-commit install
else
  echo "⚠️  pre-commit is not installed. Code quality checks will not run automatically before commits."
  echo "To install: pip install pre-commit"
fi

echo "✅ Done. Interpreter: $(python -V)."
