#!/usr/bin/env bash
# Set up the development environment (respects pyproject requires-python; safe venv upgrade)
set -euo pipefail

cd "$(dirname "$0")/.."

# Help llvmlite/numba find LLVM when building from source
export LLVM_DIR="${LLVM_DIR:-/usr/local/opt/llvm@20/lib/cmake/llvm}"

# Check if uv is installed
if ! command -v uv &>/dev/null; then
    echo "❌ uv is not installed. Please install it first:"
    echo "   curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "   or visit: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

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
uv pip install -e "."
uv pip install -e ".[test]"
[[ -f requirements_all.txt ]] && uv pip install -r requirements_all.txt


# Install pre-commit hooks if pre-commit is available
if command -v pre-commit &>/dev/null; then
  pre-commit install
else
  echo "⚠️  pre-commit not available. Install with: uv pip install pre-commit"
fi

echo "✅ Done. Interpreter: $(python -V). Package manager: $(uv --version)"
echo "To activate the virtual environment, run: source $env_name/bin/activate"
