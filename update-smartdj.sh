#!/bin/sh
# Smart DJ add-on: force the store to the newest published version, install-or-update, start.
# Run inside the SSH add-on on HAOS. No args. Safe to re-run.
set -e
SLUG=94c360f5_music_assistant_smartdj
REPO_URL=https://github.com/mattamays-ai/server
AUTH="Authorization: Bearer $SUPERVISOR_TOKEN"
BASE=http://hassio

echo "== expected version (read live from GitHub dev branch)"
EXPECTED=$(curl -fsSL "https://raw.githubusercontent.com/mattamays-ai/server/dev/smartdj-addon/config.yaml" \
  | grep -o 'version: *"[^"]*"' | head -1 | cut -d'"' -f2)
echo "expected: ${EXPECTED:-UNKNOWN}"

echo "== reloading add-on store"
curl -fsS -X POST -H "$AUTH" "$BASE/store/reload" >/dev/null && echo ok || echo "reload failed"
sleep 5

store_version() {
  curl -fsS -H "$AUTH" "$BASE/store/addons/$SLUG" 2>/dev/null \
    | grep -o '"version": *"[^"]*"' | head -1 | cut -d'"' -f2
}

V=$(store_version)
echo "store offers: ${V:-unknown}"

if [ -n "$EXPECTED" ] && [ "$V" != "$EXPECTED" ]; then
  echo "== store is stale (${V:-unknown} != $EXPECTED) -> re-adding repository (fresh clone)"
  curl -fsS -X DELETE -H "$AUTH" -H "Content-Type: application/json" \
    -d "{\"repository\": \"$REPO_URL\"}" "$BASE/store/repositories" >/dev/null \
    && echo "removed" || echo "remove skipped/failed (continuing)"
  curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
    -d "{\"repository\": \"$REPO_URL\"}" "$BASE/store/repositories" >/dev/null \
    && echo "re-added" || echo "re-add FAILED - stop here and report"
  sleep 8
  curl -fsS -X POST -H "$AUTH" "$BASE/store/reload" >/dev/null || true
  sleep 5
  V=$(store_version)
  echo "store offers now: ${V:-unknown}"
fi

echo "== installed? checking"
STATE=$(curl -fsS -H "$AUTH" "$BASE/addons/$SLUG/info" 2>/dev/null | grep -o '"state": "[^"]*"' || true)
if [ -z "$STATE" ]; then
  echo "== not installed -> installing (fresh /data, onboarding wizard on first open)"
  curl -fsS -X POST -H "$AUTH" "$BASE/store/apps/$SLUG/install" >/dev/null \
    && echo ok || echo "install failed"
else
  echo "== installed -> updating in place (data preserved)"
  curl -fsS -X POST -H "$AUTH" "$BASE/store/apps/$SLUG/update" >/dev/null \
    && echo ok || echo "update failed (check version below)"
fi

sleep 3
echo "== installed version + state (want $EXPECTED)"
curl -fsS -H "$AUTH" "$BASE/addons/$SLUG/info" \
  | grep -o '"version": *"[^"]*"\|"state": *"[^"]*"\|"name": *"[^"]*"' || true

echo "== starting"
curl -fsS -X POST -H "$AUTH" "$BASE/addons/$SLUG/start" >/dev/null \
  && echo ok || echo "start failed"
sleep 20

echo "== boot log (last 40 lines)"
curl -fsS -H "$AUTH" "$BASE/addons/$SLUG/logs" | tail -n 40
