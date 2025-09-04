#!/usr/bin/env bash
# Set up the development environment (respects pyproject requires-python; safe venv upgrade)
set -euo pipefail

cd "$(dirname "$0")/.."

env_name=${1:-".venv"}

# --- test/CI hooks (no effect for normal users) ---
: "${SETUP_DRY_RUN:=0}"       # if 1: stop before venv creation/installs (prints what it would do)
: "${SETUP_SKIP_INSTALL:=0}"  # if 1: validate/activate then stop before installs
: "${SETUP_AUTO_FIX:=0}"      # if 1: auto backup+recreate venv without prompting when version is too old

# --- helper: parse requires-python from pyproject.toml (parser-first, grep fallback) ---
py_requires=""
if [[ -f pyproject.toml ]]; then
  py_requires=$(python3 - <<'PY' 2>/dev/null || true
import sys, pathlib
text = pathlib.Path("pyproject.toml").read_text("utf-8")
try:
    import tomllib as toml  # Py 3.11+
except Exception:
    try:
        import tomli as toml  # backport if present
    except Exception:
        sys.exit(1)
data = toml.loads(text)
req = (data.get("project") or {}).get("requires-python")
if req:
    print(req)
PY
) || true
  if [[ -z "$py_requires" ]]; then
    # conservative grep: handles single/double quotes, ignores spaces
    py_requires=$(grep -E '^[[:space:]]*requires-python[[:space:]]*=' -m1 pyproject.toml \
      | sed -E 's/.*requires-python[[:space:]]*=[[:space:]]*["'\'']([^"'\'']+)["'\''].*/\1/')
  fi
fi

# --- helpers: compare version spec + pick interpreters ---
check_spec_against_python() {
  local spec="$1" pybin="$2"
  [[ -z "$spec" ]] && return 0
  local lower upper v
  lower=$(grep -oE '>=\s*[0-9]+\.[0-9]+' <<<"$spec" | head -n1 | tr -d ' ' | cut -c3-)
  upper=$(grep -oE '<\s*[0-9]+'           <<<"$spec" | head -n1 | tr -d ' ' | cut -c2-)
  v=$("$pybin" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")') || return 1

  newer_or_equal() { [[ "$(printf "%s\n%s\n" "$1" "$2" | sort -V | head -n1)" == "$1" ]]; }
  older_than()     { [[ "$(printf "%s\n%s\n" "$1" "$2" | sort -V | head -n1)" == "$1" && "$1" != "$2" ]]; }

  if [[ -n "$lower" ]]; then newer_or_equal "$lower" "$v" || return 2; fi
  if [[ -n "$upper" ]]; then older_than      "$v" "$upper" || return 3; fi
  return 0
}

pick_latest_py3() {
  # list python3.X on PATH, pick highest; else fall back to python3 if present
  mapfile -t found < <(compgen -c | grep -E '^python3\.[0-9]+$' | sort -uV)
  if (( ${#found[@]} )); then
    echo "${found[-1]}"
  elif command -v python3 &>/dev/null; then
    echo "python3"
  else
    echo ""
  fi
}

pick_matching_python() {
  # choose highest python3.X (or python3) that satisfies $1
  local spec="$1"
  mapfile -t bins < <(compgen -c | grep -E '^python3\.[0-9]+$' | sort -uV)
  bins+=("python3")
  for cand in "${bins[@]}"; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if check_spec_against_python "$spec" "$cand"; then
      echo "$cand"; return 0
    fi
  done
  return 1
}

recreate_venv_with() {
  local pybin="$1" env="${2:-$env_name}"
  local backup="${env}_backup_$(date +%s)"
  echo "Backing up existing venv to '$backup'..."
  mv "$env" "$backup"
  echo "Creating virtual environment with $pybin ..."
  "$pybin" -m venv "$env"
  # shellcheck disable=SC1090
  source "$env/bin/activate"
  echo "Using venv Python: $(python -V 2>&1)"
}

# --- main flow ---
if [[ -d "$env_name" ]]; then
  echo "Virtual environment '$env_name' detected — not modifying it (yet)."
  # shellcheck disable=SC1090
  source "$env_name/bin/activate"
  echo "Using venv Python: $(python -V 2>&1)"

  if [[ -n "$py_requires" ]]; then
    if ! check_spec_against_python "$py_requires" python; then
      cur_py="$(python -V 2>&1 || true)"
      echo "❌ Existing venv ($cur_py) does not satisfy requires-python: $py_requires" >&2

      if [[ "$SETUP_AUTO_FIX" == "1" ]]; then
        if match_bin="$(pick_matching_python "$py_requires")"; then
          recreate_venv_with "$match_bin" "$env_name"
        else
          echo "No suitable interpreter on PATH satisfies $py_requires." >&2
          echo "Ubuntu hint: sudo apt install -y python3.12 python3.12-venv" >&2
          exit 2
        fi
      else
        read -r -p "Backup and recreate '$env_name' with a matching interpreter now? [y/N] " reply
        case "$reply" in
          [yY]|[yY][eE][sS])
            if match_bin="$(pick_matching_python "$py_requires")"; then
              recreate_venv_with "$match_bin" "$env_name"
            else
              echo "No suitable interpreter on PATH satisfies $py_requires." >&2
              echo "Try: sudo apt install -y python3.12 python3.12-venv  (then re-run)." >&2
              exit 2
            fi
            ;;
          *)
            echo "Okay — leaving the existing venv untouched." >&2
            echo "Manual fix: rm -rf $env_name && python3.12 -m venv $env_name" >&2
            exit 2
            ;;
        esac
      fi
    fi
  fi

  if [[ "$SETUP_SKIP_INSTALL" == "1" ]]; then
    echo "[SKIP-INSTALL] venv validated; stopping before installs."
    exit 0
  fi

else
  # No venv: select interpreter (honour requires-python if present)
  PYTHON="$(pick_latest_py3)"
  [[ -z "$PYTHON" ]] && { echo "Error: no python3 found on PATH." >&2; exit 1; }
  echo "Selected interpreter: $PYTHON ($( $PYTHON -V 2>&1 ))"

  if [[ -n "$py_requires" ]]; then
    if ! check_spec_against_python "$py_requires" "$PYTHON"; then
      echo "❌ $PYTHON ($( $PYTHON -V 2>&1 )) does not satisfy requires-python: $py_requires" >&2
      if match_bin="$(pick_matching_python "$py_requires")"; then
        PYTHON="$match_bin"
        echo "→ Using matching interpreter: $PYTHON ($( $PYTHON -V 2>&1 ))"
      else
        echo "No suitable interpreter on PATH satisfies $py_requires." >&2
        echo "Ubuntu hint: sudo apt install -y python3.12 python3.12-venv" >&2
        exit 2
      fi
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

# --- installs ---
echo "Installing development dependencies..."
python -m pip install --upgrade pip
python -m pip install --upgrade uv
uv pip install -e "."
uv pip install -e ".[test]"
[[ -f requirements_all.txt ]] && uv pip install -r requirements_all.txt

command -v pre-commit &>/dev/null && pre-commit install

echo "✅ Done. Interpreter: $(python -V)."
echo "To activate the venv: source $env_name/bin/activate"
