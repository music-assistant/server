#!/usr/bin/env bash
# Set up the development environment (respects pyproject requires-python; safe venv upgrade)
set -euo pipefail

cd "$(dirname "$0")/.."

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

echo "Preparing AirPlay development binary..."
if ! python -m scripts.fetch_airplay_cli; then
  echo "⚠️ AirPlay binary unavailable; continuing without local AirPlay support." >&2
fi

echo "Installing development dependencies..."
uv pip install -e "."
uv pip install -e ".[test]"
# --index-strategy: allow PyPI packages when also using the PyTorch extra index
# https://docs.astral.sh/uv/pip/compatibility/#packages-that-exist-on-multiple-indexes
# Keep urllib3-future from hijacking the urllib3 namespace (see pyproject.toml).
export URLLIB3_NO_OVERRIDE=1
[[ -f requirements_all.txt ]] && uv pip install --index-strategy unsafe-best-match -r requirements_all.txt


# Install pre-commit hooks if pre-commit is available
if command -v pre-commit &>/dev/null; then
  pre-commit install
else
  echo "⚠️  pre-commit not available. Install with: uv pip install pre-commit"
fi

missing_prereqs=0
brew_lib_dir=""
if [[ "$(uname -s)" == "Darwin" ]] && command -v brew &>/dev/null; then
  brew_lib_dir="$(brew --prefix 2>/dev/null)/lib" || brew_lib_dir=""
fi

# Report a missing OS-level dependency with the install command for this platform.
warn_missing() {
  local reason=$1 brew_pkg=$2 apt_pkg=$3
  echo "⚠️  $reason"
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "    Install it with: sudo apt-get install $apt_pkg"
  elif command -v brew &>/dev/null; then
    echo "    Install it with: brew install $brew_pkg"
  else
    echo "    Install Homebrew from https://brew.sh, then: brew install $brew_pkg"
  fi
  missing_prereqs=1
}

# Report a Python binding that is installed but cannot load its native library.
check_native_lib() {
  local module=$1 brew_pkg=$2 apt_pkg=$3
  # Only meaningful once requirements_all.txt has installed the binding itself.
  if ! python -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('$module') else 1)" 2>/dev/null; then
    return
  fi
  if python -c "import $module" 2>/dev/null; then
    return
  fi
  # macOS strips DYLD_* before this script starts, so retry against the Homebrew prefix
  # to tell "library absent" apart from "library present but off the loader path".
  if [[ -n "$brew_lib_dir" ]] &&
    DYLD_FALLBACK_LIBRARY_PATH="$brew_lib_dir:/usr/local/lib:/usr/lib" python -c "import $module" 2>/dev/null; then
    echo "⚠️  '$module' only finds its native library through the Homebrew prefix."
    echo "    Make sure your shell profile exports this (this script cannot see it):"
    echo "      export DYLD_FALLBACK_LIBRARY_PATH=\"\$(brew --prefix)/lib:/usr/local/lib:/usr/lib\""
    missing_prereqs=1
    return
  fi
  warn_missing "'$module' is installed but cannot load its native library." "$brew_pkg" "$apt_pkg"
}

# Both providers import these bindings lazily, so a missing native library only stops the
# provider that needs it. Check here so that shows up during setup instead of at runtime.
echo "Checking OS-level dependencies..."
command -v ffmpeg &>/dev/null || warn_missing "ffmpeg not found in PATH (6.1 minimum, 7.x recommended)." ffmpeg ffmpeg
check_native_lib chromaprint chromaprint libchromaprint1
check_native_lib sounddevice portaudio libportaudio2

if [[ "$missing_prereqs" -eq 1 ]]; then
  echo "⚠️  Some tests and providers may not work until the above is resolved (see DEVELOPMENT.md)."
fi

echo "✅ Done. Interpreter: $(python -V). Package manager: $(uv --version)"
echo "To activate the virtual environment, run: source $env_name/bin/activate"
