#!/usr/bin/env bash
# Update this checkout and (re)build + start the Music Assistant dev container.
#
# Run on the Unraid host from anywhere inside the checkout, e.g.:
#   /mnt/user/appdata/music-assistant-dev-src/scripts/unraid/deploy.sh
# Overrides: MA_DEV_PORT (8195), MA_DEV_STREAM_PORT (8197), MA_DEV_DATA, MA_IMAGE.
set -euo pipefail

cd "$(dirname "$0")"

if [[ "${1:-}" != "--no-pull" ]]; then
  git -C ../.. pull --ff-only
fi
docker compose pull --ignore-buildable 2>/dev/null || true
docker compose build --pull
docker compose up -d
echo "Music Assistant dev is starting on http://$(hostname -I | awk '{print $1}'):${MA_DEV_PORT:-8195}"
echo "Follow the log with: docker logs -f music-assistant-dev"
