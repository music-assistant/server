#!/bin/sh
# install-smartdj-ma.sh — one-shot installer for the Smart DJ Music Assistant instance
#
# Installs the official "Music Assistant DEV SERVER" add-on (the one that builds
# whatever server_repo/frontend_repo you point it at), configures it to build
# Matthew's fork (Smart DJ on server dev + frontend main), and starts it.
#
# WHERE TO RUN: on Home Assistant OS itself — via the "Terminal & SSH" add-on
# (Settings → Add-ons → Terminal & SSH) or the VM console. Needs SUPERVISOR_TOKEN,
# which is present in every HAOS shell session.
#
# USAGE:
#   sh install-smartdj-ma.sh            # interactive (asks before stopping nightly)
#   sh install-smartdj-ma.sh --yes      # don't ask, just go
#   sh install-smartdj-ma.sh --logs     # just follow the dev add-on's logs live
#   sh install-smartdj-ma.sh --rollback # stop dev, restart nightly
#
# Everything it does is reversible: the nightly add-on is never uninstalled.

set -eu

SERVER_REPO="mattamays-ai/server@dev"
FRONTEND_REPO="mattamays-ai/mattamays-ai-music-assistant-frontend@main"
SLUG="music_assistant_dev"
NIGHTLY_SLUG="music_assistant_nightly"
HASSIO="http://hassio"

[ -n "${SUPERVISOR_TOKEN:-}" ] || {
  echo "ERROR: SUPERVISOR_TOKEN not set."
  echo "Run this from a shell on Home Assistant OS (Terminal & SSH add-on or VM console),"
  echo "not from an arbitrary machine."
  exit 1
}

api() { # api METHOD PATH [JSON]
  method=$1; path=$2; body=${3:-}
  if [ -n "$body" ]; then
    curl -fsS -X "$method" -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
      -H "Content-Type: application/json" -d "$body" "$HASSIO$path"
  else
    curl -fsS -X "$method" -H "Authorization: Bearer $SUPERVISOR_TOKEN" "$HASSIO$path"
  fi
  echo
}

die() { echo "ERROR: $1" >&2; exit 1; }
jsonval() { # crude "did result=ok" check
  echo "$1" | grep -q '"result": *"ok"' || echo "$1" | grep -q '"result":"ok"'
}

ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1
if [ "${1:-}" = "--logs" ]; then
  echo "==> Following $SLUG logs live (Ctrl-C to stop; the add-on keeps running)"
  exec curl -fsS -N -H "Authorization: Bearer $SUPERVISOR_TOKEN" "$HASSIO/addons/$SLUG/logs/follow"
fi
if [ "${1:-}" = "--rollback" ]; then
  echo "==> Stopping $SLUG (if installed) and restarting $NIGHTLY_SLUG"
  api POST "/addons/$SLUG/stop" >/dev/null 2>&1 || true
  api POST "/addons/$NIGHTLY_SLUG/start" >/dev/null 2>&1 || true
  echo "==> Rollback done. Nightly is your Music Assistant again."
  exit 0
fi

echo "==> Smart DJ Music Assistant installer"
echo "    server_repo:   $SERVER_REPO"
echo "    frontend_repo: $FRONTEND_REPO"
echo "    add-on slug:   $SLUG (separate instance; your nightly is untouched)"
echo ""

# 1. make sure the official add-on repository is registered
echo "==> [1/5] Checking add-on store repositories"
repos=$(api GET "/store/repositories") || die "could not reach the Supervisor API"
if ! echo "$repos" | grep -q "home-assistant-addon"; then
  echo "    adding https://github.com/music-assistant/home-assistant-addon"
  api POST "/store/repositories" '{"repository":"https://github.com/music-assistant/home-assistant-addon"}' >/dev/null \
    || die "could not add the music-assistant add-on repository"
  echo "    forcing a store sync"
  api POST "/store/reload" >/dev/null 2>&1 || true
  sleep 5
fi
echo "    ok"

# 2. install the dev app (no-op if already installed), then PROVE it exists
echo "==> [2/5] Installing $SLUG (skips if present; first download is a few hundred MB)"
resp=$(api POST "/store/apps/$SLUG/install" 2>&1) || {
  echo "$resp" | grep -qi "already" || die "install failed: $resp"
}
echo "    install call ok — verifying the add-on actually exists"
resp=$(api GET "/addons/$SLUG/info" 2>&1) || {
  echo "    404 on /addons/$SLUG/info — installed add-ons found:"
  api GET "/addons" 2>/dev/null | tr ',' '\n' | grep '"slug"' || true
  api GET "/store/apps" 2>/dev/null | tr ',' '\n' | grep -i "music_assistant" || true
  die "add-on $SLUG is not installed after the install call. Paste the lines above."
}
jsonval "$resp" || die "add-on info looked wrong: $resp"
echo "    add-on present"

# 3. point it at the fork
echo "==> [3/5] Configuring server/frontend sources"
resp=$(api POST "/addons/$SLUG/options" \
  "{\"options\":{\"server_repo\":\"$SERVER_REPO\",\"frontend_repo\":\"$FRONTEND_REPO\"}}") \
  || die "setting options failed: $resp"
jsonval "$resp" || die "setting options failed: $resp"
echo "    ok"

# 4. stop nightly (both bind the same host network ports)
if [ "$ASSUME_YES" -ne 1 ]; then
  printf "Stop the nightly Music Assistant add-on while the dev one runs? [y/N] "
  read -r answer
  case "$answer" in y|Y) ASSUME_YES=1;; esac
fi
if [ "$ASSUME_YES" -eq 1 ]; then
  echo "==> [4/5] Stopping $NIGHTLY_SLUG"
  api POST "/addons/$NIGHTLY_SLUG/stop" >/dev/null 2>&1 && echo "    stopped" || echo "    (not installed or already stopped — continuing)"
else
  echo "==> [4/5] Leaving nightly alone — NOTE: it will conflict on ports until you stop it"
fi

# 5. start
echo "==> [5/5] Starting $SLUG"
resp=$(api POST "/addons/$SLUG/start") || die "start failed: $resp"
jsonval "$resp" || die "start failed: $resp"
echo "    started"
echo ""
# 6. show the first logs so the build is visible
echo "==> Waiting 20s for the first log lines..."
sleep 20
echo "---- last 40 log lines ($(date +%H:%M:%S)) ----"
api GET "/addons/$SLUG/logs" | tail -n 40 || echo "(could not fetch logs yet — try: sh install-smartdj-ma.sh --logs)"
echo "---- end ----"
echo ""
echo "==> First boot builds your fork's server + frontend inside the add-on:"
echo "    expect 5-15 minutes before the UI responds. Keep watching with:"
echo "      sh install-smartdj-ma.sh --logs"
echo "      (or:  ha apps logs $SLUG -f   /   ha addons logs $SLUG -f on older CLI)"
echo "    Smart DJ UI:     Settings → Music Assistant panel in HA"
echo "    Rollback:        sh install-smartdj-ma.sh --rollback"
