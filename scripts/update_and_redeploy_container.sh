#!/usr/bin/env bash
set -euo pipefail

# Update repo, build Python artifact, build Docker image, and redeploy local container.
# Override defaults by exporting env vars before running.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

CONTAINER_NAME="${CONTAINER_NAME:-music-assistant-local}"
IMAGE_NAME="${IMAGE_NAME:-ma-server-local}"
MASS_VERSION="${MASS_VERSION:-0.0.0}"
NETWORK_MODE="${NETWORK_MODE:-host}"

DATA_DIR="${DATA_DIR:-/store/home-assistant/mass_data}"
MUSIC_DIR="${MUSIC_DIR:-/mnt/crypt/ssd_1tb_nvme/store/copyparty/martin/MP3s/}"
TIMEZONE_FILE="${TIMEZONE_FILE:-/etc/localtime}"

echo "[1/5] Pulling latest changes (ff-only)..."
git pull --ff-only

echo "[2/5] Building Python distribution (dist/)..."
if ! command -v uv >/dev/null 2>&1; then
  echo "Error: 'uv' not found in PATH"
  exit 1
fi

uv pip install build tomli tomli-w
python -m build

echo "[3/5] Building Docker image: ${IMAGE_NAME} (MASS_VERSION=${MASS_VERSION})..."
docker build -t "$IMAGE_NAME" --build-arg MASS_VERSION="$MASS_VERSION" .

echo "[4/5] Removing old container if present: ${CONTAINER_NAME}..."
if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  docker rm -f "$CONTAINER_NAME" >/dev/null
fi

echo "[5/5] Starting new container: ${CONTAINER_NAME}..."
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --network "$NETWORK_MODE" \
  -v "$DATA_DIR:/data" \
  -v "$MUSIC_DIR:/copyparty-music/" \
  -v "$TIMEZONE_FILE:/etc/localtime:ro" \
  "$IMAGE_NAME" >/dev/null

echo "Done. Container '${CONTAINER_NAME}' is running with image '${IMAGE_NAME}'."
