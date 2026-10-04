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
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY --from=fewheel /wheels /wheels
COPY requirements_all.txt MANIFEST.in /build/
COPY pyproject.toml README.md setup.cfg /build/
COPY music_assistant /build/music_assistant
# NOTE: no app_secrets.json handling here — that file is a nightly-only artifact;
# the fork server never references it (verified: zero `app_secrets` hits in fork source).
RUN . /app/venv/bin/activate \
  && export UV_INDEX_STRATEGY=unsafe-best-match \
  && uv pip install --python /app/venv/bin/python --no-cache -r /build/requirements_all.txt \
  && uv pip install --python /app/venv/bin/python --no-cache /build \
  && uv pip install --python /app/venv/bin/python --no-cache /wheels/*.whl \
  && rm -rf /build /wheels /root/.cache
EXPOSE 8095 8097
ENTRYPOINT ["/usr/local/bin/entrypoint.sh", "--data-dir", "/data", "--cache-dir", "/data/.cache"]
