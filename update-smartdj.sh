#!/bin/sh
# Smart DJ add-on: force store sync, update to the newest image, restart, show log.
# Run inside the SSH add-on on HAOS. No args.
set -e
SLUG=94c360f5_music_assistant_smartdj
AUTH="Authorization: Bearer $SUPERVISOR_TOKEN"
BASE=http://hassio

echo "== reloading add-on store"
curl -fsS -X POST -H "$AUTH" "$BASE/store/reload" >/dev/null && echo ok || echo "reload failed"
sleep 5

echo "== updating add-on (pulls the 0.0.2 image)"
curl -fsS -X POST -H "$AUTH" "$BASE/store/apps/$SLUG/update" >/dev/null \
  && echo ok || echo "update failed (check version below)"

sleep 3
echo "== add-on version + state"
curl -fsS -H "$AUTH" "$BASE/addons/$SLUG/info" \
  | grep -o '"version": "[^"]*"\|"state": "[^"]*"\|"name": "[^"]*"' || true

echo "== starting"
curl -fsS -X POST -H "$AUTH" "$BASE/addons/$SLUG/start" >/dev/null \
  && echo ok || echo "start failed"
sleep 20

echo "== boot log (last 40 lines)"
curl -fsS -H "$AUTH" "$BASE/addons/$SLUG/logs" | tail -n 40
