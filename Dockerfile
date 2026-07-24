# syntax=docker/dockerfile:1

ARG BASE_IMAGE_VERSION=latest
FROM --platform=$BUILDPLATFORM ghcr.io/music-assistant/base:$BASE_IMAGE_VERSION AS cliairplay-download

# Bump the version and checksum-manifest hash together.
ARG CLIAIRPLAY_VERSION=v0.3.1
ARG CLIAIRPLAY_CHECKSUMS_SHA256=53909d70f38f90c7218c718f71b8d272a67faa043ad8a0a046a2cb7178662500
ARG TARGETARCH

# Download the cliairplay release asset for this image architecture.
RUN set -eu \
    && case "$TARGETARCH" in \
        amd64) CLIAIRPLAY_ARCH="x86_64" ;; \
        arm64) CLIAIRPLAY_ARCH="aarch64" ;; \
        *) echo "Unsupported cliairplay architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && CLIAIRPLAY_BINARY="cliairplay-linux-${CLIAIRPLAY_ARCH}" \
    && RELEASE_URL="https://github.com/music-assistant/airplay-cli/releases/download/${CLIAIRPLAY_VERSION}" \
    && wget -q "${RELEASE_URL}/SHA256SUMS" -O /tmp/SHA256SUMS \
    && wget -q "${RELEASE_URL}/${CLIAIRPLAY_BINARY}" -O "/tmp/${CLIAIRPLAY_BINARY}" \
    && echo "${CLIAIRPLAY_CHECKSUMS_SHA256}  /tmp/SHA256SUMS" | sha256sum --check - \
    && mkdir -p /cliairplay \
    && mv "/tmp/${CLIAIRPLAY_BINARY}" "/cliairplay/${CLIAIRPLAY_BINARY}" \
    && awk -v filename="$CLIAIRPLAY_BINARY" \
        '$2 == filename || $2 == "*" filename' \
        /tmp/SHA256SUMS > /tmp/cliairplay.sha256 \
    && test "$(wc -l < /tmp/cliairplay.sha256)" -eq 1 \
    && (cd /cliairplay && sha256sum --check /tmp/cliairplay.sha256) \
    && chmod 755 "/cliairplay/${CLIAIRPLAY_BINARY}" \
    && rm /tmp/SHA256SUMS /tmp/cliairplay.sha256

FROM scratch AS cliairplay
COPY --from=cliairplay-download /cliairplay /cliairplay

# Builder image. It builds the venv that will be copied to the final image
#
FROM ghcr.io/music-assistant/base:$BASE_IMAGE_VERSION AS builder
ARG TARGETARCH

ADD dist dist
COPY requirements_all.txt .

# miniaudio has no Linux arm64 wheels, so pyatv requires a source build there.
# The compiler stays in this disposable builder stage and is not copied to the final image.
RUN if [ "$TARGETARCH" = "arm64" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends gcc g++ && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# ensure UV is installed
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# create venv which will be copied to the final image
ENV VIRTUAL_ENV=/app/venv
RUN uv venv $VIRTUAL_ENV

# pre-install ALL requirements into the venv
# comes at a cost of a slightly larger image size but is faster to start
# because we do not have to install dependencies at runtime
# --index-strategy: allow PyPI packages when also using the PyTorch extra index
# https://docs.astral.sh/uv/pip/compatibility/#packages-that-exist-on-multiple-indexes
# Keep urllib3-future from hijacking the urllib3 namespace (see pyproject.toml).
ENV URLLIB3_NO_OVERRIDE=1
RUN uv pip install \
    --index-strategy unsafe-best-match \
    --no-binary urllib3-future \
    -r requirements_all.txt

# Install PyAV from pre-built wheel (built against system FFmpeg in base image)
# First verify the wheel version matches what pip resolved to avoid version mismatch
RUN REQUIRED_VERSION=$($VIRTUAL_ENV/bin/python -c "import importlib.metadata; print(importlib.metadata.version('av'))") && \
    WHEEL_VERSION=$(ls /usr/local/share/pyav-wheels/av*.whl | grep -oP 'av-\K[0-9.]+') && \
    if [ "$REQUIRED_VERSION" != "$WHEEL_VERSION" ]; then \
      echo "ERROR: PyAV version mismatch! Requirements need $REQUIRED_VERSION but base image has $WHEEL_VERSION" && \
      echo "Please rebuild the base image with the correct PyAV version." && \
      exit 1; \
    fi && \
    uv pip install --force-reinstall --no-deps /usr/local/share/pyav-wheels/av*.whl

# Install Music Assistant from prebuilt wheel
ARG MASS_VERSION
RUN uv pip install \
    --no-cache \
    "music-assistant@dist/music_assistant-${MASS_VERSION}-py3-none-any.whl"

COPY --from=cliairplay /cliairplay /tmp/cliairplay
RUN SITE_PACKAGES="$("$VIRTUAL_ENV/bin/python" -c \
        'import sysconfig; print(sysconfig.get_path("purelib"))')" \
    && CLIAIRPLAY_BIN_DIR="${SITE_PACKAGES}/music_assistant/providers/airplay/bin" \
    && mkdir -p "$CLIAIRPLAY_BIN_DIR" \
    && mv /tmp/cliairplay/* "$CLIAIRPLAY_BIN_DIR/" \
    && rmdir /tmp/cliairplay \
    && "$CLIAIRPLAY_BIN_DIR"/cliairplay-linux-* --check

# Pre-compile Python bytecode for faster startup
RUN $VIRTUAL_ENV/bin/python -m compileall -q $VIRTUAL_ENV/lib/python*/site-packages/music_assistant

# we need to set (very permissive) permissions to the workdir
# and /tmp to allow running the container as non-root
# IMPORTANT: chmod here, NOT on the final image, to avoid creating extra layers and increase size!
#
RUN chmod -R 777 /app \
    && chmod 755 \
        "$VIRTUAL_ENV"/lib/python*/site-packages/music_assistant/providers/airplay/bin/cliairplay-linux-*

##################################################################################################

# FINAL docker image for music assistant server

FROM ghcr.io/music-assistant/base:$BASE_IMAGE_VERSION

ENV VIRTUAL_ENV=/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# copy the already built /app dir
COPY --from=builder /app /app

# the /app contents have correct permissions but for some reason /app itself does not.
# so apply again, but ONLY to the dir (otherwise we increase the size)
RUN chmod 777 /app

# Set some labels
ARG MASS_VERSION
ARG TARGETPLATFORM
LABEL \
    org.opencontainers.image.title="Music Assistant Server" \
    org.opencontainers.image.description="Music Assistant is a free, opensource Media library manager that connects to your streaming services and a wide range of connected speakers. The server is the beating heart, the core of Music Assistant and must run on an always-on device like a Raspberry Pi, a NAS or an Intel NUC or alike." \
    org.opencontainers.image.source="https://github.com/music-assistant/server" \
    org.opencontainers.image.authors="The Music Assistant Team" \
    org.opencontainers.image.documentation="https://music-assistant.io" \
    org.opencontainers.image.licenses="Apache License 2.0" \
    io.hass.version="${MASS_VERSION}" \
    io.hass.type="addon" \
    io.hass.name="Music Assistant Server" \
    io.hass.description="Music Assistant Server" \
    io.hass.platform="${TARGETPLATFORM}" \
    io.hass.type="addon"

VOLUME [ "/data" ]
EXPOSE 8095

WORKDIR $VIRTUAL_ENV

# Entrypoint script that enables jemalloc for the main process only.
# MALLOC_CONF enables jemalloc's background thread so freed pages are returned to
# the OS while the process is idle; without it an idle server holds onto the peak
# RSS reached during startup (db migration, provider setup, metadata, image decode).
RUN printf '#!/bin/sh\n\
for path in /usr/lib/*/libjemalloc.so.2; do\n\
    [ -f "$path" ] && export LD_PRELOAD="$path" MALLOC_CONF="background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000" && break\n\
done\n\
exec mass "$@"\n' > /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh", "--data-dir", "/data", "--cache-dir", "/data/.cache"]
