#!/usr/bin/env bash
set -euo pipefail

# Update repo, build Python artifact, build Docker image, and redeploy local container.
# Override defaults by exporting env vars before running.
# Optional argument:
#   ./scripts/update_and_redeploy_container.sh /path/to/ma-frontend
# If provided, the script will build a local frontend wheel and temporarily replace
# '../ma-frontend' in requirements_all.txt with the wheel path for Docker build.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

FRONTEND_PATH_ARG="${1:-${FRONTEND_PATH:-}}"
if [[ $# -gt 1 ]]; then
  echo "Usage: $0 [frontend_path]"
  exit 1
fi

CONTAINER_NAME="${CONTAINER_NAME:-music-assistant-local}"
IMAGE_NAME="${IMAGE_NAME:-ma-server-local}"
MASS_VERSION="${MASS_VERSION:-0.0.0}"
NETWORK_MODE="${NETWORK_MODE:-host}"

DATA_DIR="${DATA_DIR:-/path/to/mass_data}"
MUSIC_DIR="${MUSIC_DIR:-/path/to/music}"
TIMEZONE_FILE="${TIMEZONE_FILE:-/path/to/timezone/file}"

TOTAL_STEPS=5
if [[ -n "$FRONTEND_PATH_ARG" ]]; then
  TOTAL_STEPS=6
fi

STEP=1
echo "[${STEP}/${TOTAL_STEPS}] Pulling latest changes (ff-only)..."
git pull --ff-only
STEP=$((STEP + 1))

echo "[${STEP}/${TOTAL_STEPS}] Building Python distribution (dist/)..."
if ! command -v uv >/dev/null 2>&1; then
  echo "Error: 'uv' not found in PATH"
  exit 1
fi

uv pip install build tomli tomli-w
python -m build
STEP=$((STEP + 1))

REQUIREMENTS_FILE="$REPO_ROOT/requirements_all.txt"
REQUIREMENTS_BACKUP=""
PATCHED_REQUIREMENTS=""

cleanup() {
  if [[ -n "$REQUIREMENTS_BACKUP" && -f "$REQUIREMENTS_BACKUP" ]]; then
    mv "$REQUIREMENTS_BACKUP" "$REQUIREMENTS_FILE"
  fi
  if [[ -n "$PATCHED_REQUIREMENTS" && -f "$PATCHED_REQUIREMENTS" ]]; then
    rm -f "$PATCHED_REQUIREMENTS"
  fi
}
trap cleanup EXIT

if [[ -n "$FRONTEND_PATH_ARG" ]]; then
  FRONTEND_ROOT="$(cd "$FRONTEND_PATH_ARG" && pwd)"
  if [[ ! -d "$FRONTEND_ROOT" ]]; then
    echo "Error: frontend path not found: $FRONTEND_PATH_ARG"
    exit 1
  fi

  echo "[${STEP}/${TOTAL_STEPS}] Building local frontend wheel from: ${FRONTEND_ROOT}"
  (
    cd "$FRONTEND_ROOT"
    uv build
  )
  FRONTEND_WHEEL="$(ls -1t "$FRONTEND_ROOT"/dist/music_assistant_frontend-*.whl 2>/dev/null | head -n 1 || true)"
  if [[ -z "$FRONTEND_WHEEL" ]]; then
    echo "Error: no frontend wheel found in $FRONTEND_ROOT/dist"
    exit 1
  fi

  mkdir -p "$REPO_ROOT/dist"
  cp "$FRONTEND_WHEEL" "$REPO_ROOT/dist/"
  FRONTEND_WHEEL_NAME="$(basename "$FRONTEND_WHEEL")"

  REQUIREMENTS_BACKUP="$(mktemp "$REPO_ROOT/requirements_all.txt.backup.XXXXXX")"
  cp "$REQUIREMENTS_FILE" "$REQUIREMENTS_BACKUP"
  PATCHED_REQUIREMENTS="$(mktemp "$REPO_ROOT/requirements_all.txt.patched.XXXXXX")"

  if ! awk -v wheel_path="dist/${FRONTEND_WHEEL_NAME}" '
    $0 == "../ma-frontend" {
      print wheel_path
      replaced = 1
      next
    }
    { print }
    END {
      if (!replaced) {
        exit 2
      }
    }
  ' "$REQUIREMENTS_FILE" > "$PATCHED_REQUIREMENTS"; then
    echo "Error: requirements_all.txt does not contain exact line '../ma-frontend'"
    echo "       Keep that line for local frontend builds, then rerun."
    exit 1
  fi

  mv "$PATCHED_REQUIREMENTS" "$REQUIREMENTS_FILE"
  PATCHED_REQUIREMENTS=""
  STEP=$((STEP + 1))
fi

echo "[${STEP}/${TOTAL_STEPS}] Building Docker image: ${IMAGE_NAME} (MASS_VERSION=${MASS_VERSION})..."
docker build -t "$IMAGE_NAME" --build-arg MASS_VERSION="$MASS_VERSION" .
STEP=$((STEP + 1))

echo "[${STEP}/${TOTAL_STEPS}] Removing old container if present: ${CONTAINER_NAME}..."
if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  docker rm -f "$CONTAINER_NAME" >/dev/null
fi
STEP=$((STEP + 1))

echo "[${STEP}/${TOTAL_STEPS}] Starting new container: ${CONTAINER_NAME}..."
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --network "$NETWORK_MODE" \
  -v "$DATA_DIR:/data" \
  -v "$MUSIC_DIR:/copyparty-music/" \
  -v "$TIMEZONE_FILE:/etc/localtime:ro" \
  "$IMAGE_NAME" >/dev/null

echo "Done. Container '${CONTAINER_NAME}' is running with image '${IMAGE_NAME}'."
