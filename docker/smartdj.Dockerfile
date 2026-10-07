# syntax=docker/dockerfile:1
#
# Smart DJ image: the official nightly server image, with mattamays-ai's fork
# server (Smart DJ provider) and fork frontend baked in at build time.
# Built by .github/workflows/smartdj-image.yml and published to ghcr.io/mattamays-ai/server.
#
# Result for the HA add-on: a prebuilt image with NO per-boot source builds —
# restarts take seconds instead of re-downloading torch and rebuilding the frontend.

ARG FE_REF=main

# ---- stage 1: build the fork frontend into a python wheel -------------------
FROM node:22-alpine AS fe
ARG FE_REF
RUN apk add --no-cache git
RUN git clone --depth 1 --branch ${FE_REF} \
      https://github.com/mattamays-ai/mattamays-ai-music-assistant-frontend /fe
WORKDIR /fe
RUN corepack enable
RUN pnpm install --frozen-lockfile --store-dir .pnpm-store
RUN ./node_modules/.bin/vite build
RUN rm -rf .pnpm-store node_modules

# ---- stage 2: wheel the built frontend --------------------------------------
FROM python:3.14-slim AS fewheel
COPY --from=fe /fe /fe
RUN pip wheel --no-deps -w /wheels /fe

# ---- stage 3: overlay fork server + fork frontend onto the nightly base -----
FROM ghcr.io/music-assistant/server:nightly
# PortAudio for sounddevice (local_audio provider + Sendspin); the base image lacks it
RUN apt-get update && apt-get install -y --no-install-recommends libportaudio2 git \
  && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY --from=fewheel /wheels /wheels
COPY requirements_all.txt MANIFEST.in /build/
COPY pyproject.toml README.md setup.cfg /build/
COPY music_assistant /build/music_assistant
# Preserve the base image's bundled app_secrets.json (upstream's private
# provider credentials: spotify_client_id, tidal, deezer, qobuz, apple_music_token,
# lastfm, acoustid — consumed at runtime by music_assistant/helpers/app_vars.py).
# Reinstalling the fork's music_assistant package replaces the base install and
# would otherwise delete this file, leaving app_var() empty and every provider
# that relies on bundled credentials broken at setup ("client_id: Not present").
# Fail loudly if upstream stops shipping it — a silent skip re-breaks providers.
RUN set -eux; \
  src="$(find /app/venv -name app_secrets.json -path '*music_assistant*')"; \
  mkdir -p /tmp/appvars; \
  cp "$src" /tmp/app_secrets.json; \
  dirname "$src" > /tmp/appvars/destdir
RUN . /app/venv/bin/activate \
  && export UV_INDEX_STRATEGY=unsafe-best-match \
  && uv pip install --python /app/venv/bin/python --no-cache -r /build/requirements_all.txt \
  && uv pip install --python /app/venv/bin/python --no-cache /build \
  && uv pip install --python /app/venv/bin/python --no-cache /wheels/*.whl \
  && rm -rf /build /wheels /root/.cache
# Restore the bundled app secrets into the reinstalled package tree.
RUN set -eux; \
  destdir="$(cat /tmp/appvars/destdir)"; \
  cp /tmp/app_secrets.json "$destdir/app_secrets.json"; \
  rm -rf /tmp/appvars /tmp/app_secrets.json
EXPOSE 18095 18097

# Admin-reset hook: wrap the base entrypoint. If the add-on option reset_admin
# is true, archive the auth database once (rising-edge, marker-file guarded) so
# the /setup onboarding page comes back on next start. Library, players,
# settings and provider tokens live in other files and are NOT touched.
# Flip the option off (or on->off->on for a later reset) to re-arm.
RUN mv /usr/local/bin/entrypoint.sh /usr/local/bin/entrypoint-orig.sh
COPY <<'EOF' /usr/local/bin/entrypoint.sh
#!/bin/sh
/app/venv/bin/python - <<'PY'
import json, os, shutil, time
data_dir = "/data"
opts_path = os.path.join(data_dir, "options.json")
opts = {}
if os.path.exists(opts_path):
    try:
        with open(opts_path) as f:
            opts = json.load(f)
    except Exception:
        opts = {}
marker = os.path.join(data_dir, ".auth_reset_done")
if not opts.get("reset_admin"):
    if os.path.exists(marker):
        os.remove(marker)
        print("[smartdj] reset_admin off: re-armed for next time", flush=True)
elif not os.path.exists(marker):
    ts = time.strftime("%Y%m%d-%H%M%S")
    for name in ("auth.db", "auth.db-wal", "auth.db-shm"):
        fp = os.path.join(data_dir, name)
        if os.path.exists(fp):
            shutil.move(fp, fp + ".bak-" + ts)
    with open(marker, "w") as f:
        f.write(ts)
    print("[smartdj] reset_admin: auth database archived, onboarding will be offered", flush=True)
PY
exec /usr/local/bin/entrypoint-orig.sh "$@"
EOF
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh", "--data-dir", "/data", "--cache-dir", "/data/.cache"]
