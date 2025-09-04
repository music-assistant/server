#!/usr/bin/env bash
# Set up the development environment (no venv overwrite; honours pyproject requires-python)
set -euo pipefail

: "${SETUP_DRY_RUN:=0}"       # if 1: stop before creating venv / installing
: "${SETUP_SKIP_INSTALL:=0}"  # if 1: stop after selecting/validating interpreter

cd "$(dirname "$0")/.."

env_name=${1:-".venv"}

# --- read requires-python from pyproject.toml if present ---
py_requires=""
if [[ -f pyproject.toml ]]; then
  # Prefer tomllib (Python 3.11+); fall back to a simple grep
  py_requires=$(python3 - <<'PY' 2>/dev/null || true
import sys, pathlib
try:
    import tomllib
except Exception:
    sys.exit(1)
data = tomllib.loads(pathlib.Path("pyproject.toml").read_text("utf-8"))
req = (data.get("project") or {}).get("requires-python")
if req:
    print(req)
PY
)
  if [[ -z "$py_requires" ]]; then
    py_requires=$(grep -E '^\s*requires-python\s*=\s*"[^\"]+"' -m1 pyproject.toml \
      | sed -E 's/.*requires-python\s*=\s*"([^"]+)".*/\1/')
  fi
fi

# Parse a spec like '>=3.12,<4' (common cases). If exotic, we won't block—uv will.
check_spec_against_python() {
  local spec="$1" pybin="$2"
  [[ -z "$spec" ]] && return 0
  local lower upper v
  lower=$(grep -oE '>=\s*[0-9]+\.[0-9]+' <<<"$spec" | head -n1 | tr -d ' ' | cut -c3-)
  upper=$(grep -oE '<\s*[0-9]+'           <<<"$spec" | head -n1 | tr -d ' ' | cut -c2-)
  v=$("$pybin" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")') || return 1
  newer_or_equal() { [[ "$(printf "%s\n%s\n" "$1" "$2" | sort -V | head -n1)" == "$1" ]]; }
  older_than()     { [[ "$(printf "%s\n%s\n" "$1" "$2" | sort -V | head -n1)" == "$1" && "$1" != "$2" ]]; }
  [[ -n "$lower" ]] && newer_or_equal "$lower" "$v" || return 2
  [[ -n "$upper" ]] && older_than      "$v" "$upper" || return 3
  return 0
}

pick_latest_py3() {
  # Find all python3.X on PATH, sort naturally, pick highest; else fall back to python3
  mapfile -t found < <(compgen -c | grep -E '^python3\.[0-9]+$' | sort -uV)
  if (( ${#found[@]} )); then
    echo "${found[-1]}"
  elif command -v python3 &>/dev/null; then
    echo "python3"
  else
    echo ""
  fi
}

# --- if venv exists: activate & validate; otherwise: select interpreter & create venv ---
if [[ -d "$env_name" ]]; then
  echo "Virtual environment '$env_name' detected so not modifying it."
  # shellcheck disable=SC1090
  source "$env_name/bin/activate"
  echo "Existing venv is using Python: $(python -V 2>&1)"

  if [[ -n "$py_requires" ]]; then
    if ! check_spec_against_python "$py_requires" python; then
      echo "❌ Existing venv Python $(python -V 2>&1) does not satisfy requires-python: $py_requires" >&2
      echo "Fix (manual): remove and recreate the venv with a matching interpreter, e.g.:" >&2
      echo "  rm -rf ${env_name} && python3.12 -m venv ${env_name}" >&2
      echo "Ubuntu hint: sudo apt install -y python3.12 python3.12-venv" >&2
      exit 2
    fi
  fi
else
  PYTHON="$(pick_latest_py3)"
  if [[ -z "$PYTHON" ]]; then
    echo "Error: No python3 interpreter found on PATH." >&2
    echo "Ubuntu hint: sudo apt update && sudo apt install -y python3.12 python3.12-venv" >&2
    exit 1
  fi
  echo "Selected interpreter: $PYTHON ($( $PYTHON -V 2>&1 ))"

  if [[ -n "$py_requires" ]]; then
    if ! check_spec_against_python "$py_requires" "$PYTHON"; then
      echo "❌ $PYTHON ($( $PYTHON -V 2>&1 )) does not satisfy requires-python: $py_requires" >&2
      echo "Install a matching Python and retry (e.g., sudo apt install python3.12 python3.12-venv)." >&2
      exit 2
    fi
  fi

if [[ "$SETUP_DRY_RUN" == "1" ]]; then
  echo "[DRY-RUN] Would create venv with: $PYTHON"
  exit 0
fi

  echo "Creating virtual environment with $PYTHON ..."
  "$PYTHON" -m venv "$env_name"
  # shellcheck disable=SC1090
  source "$env_name/bin/activate"
  echo "Using venv Python: $(python -V 2>&1)"
fi

if [[ "$SETUP_SKIP_INSTALL" == "1" ]]; then
  echo "[SKIP-INSTALL] venv validated; stopping before installs."
  exit 0
fi

echo "Installing development dependencies..."
python -m pip install --upgrade pip
python -m pip install --upgrade uv
uv pip install -e "."
uv pip install -e ".[test]"
[[ -f requirements_all.txt ]] && uv pip install -r requirements_all.txt

command -v pre-commit &>/dev/null && pre-commit install

echo "✅ Done. Interpreter: $(python -V)."
