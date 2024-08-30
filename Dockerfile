# syntax=docker/dockerfile:1

FROM python:3.12-alpine3.20

ARG MASS_VERSION
ARG TARGETPLATFORM

RUN set -x \
    && apk add --no-cache \
        ca-certificates \
        jemalloc \
        curl \
        git \
        wget \
        tzdata \
        sox \
        cifs-utils \
    # install ffmpeg from community repo
    && apk add --no-cache ffmpeg --repository=https://dl-cdn.alpinelinux.org/alpine/v3.20/community \
    # install snapcast from community repo
    && apk add --no-cache snapcast --repository=https://dl-cdn.alpinelinux.org/alpine/v3.20/community \
    # install libnfs from community repo
    && apk add --no-cache libnfs --repository=https://dl-cdn.alpinelinux.org/alpine/v3.20/community \
    # install openssl-dev (needed for airplay)
    && apk add --no-cache openssl-dev

# Copy widevine client files to container
RUN mkdir -p /usr/local/bin/widevine_cdm
COPY widevine_cdm/* /usr/local/bin/widevine_cdm/

# Upgrade pip + Install uv
RUN pip install --upgrade pip \
    && pip install uv==0.2.27

# Install Music Assistant from published wheel on PyPi
RUN uv pip install \
    --system \
    --no-cache \
    --find-links "https://wheels.home-assistant.io/musllinux/" \
    music-assistant[server]==${MASS_VERSION}

# Configure runtime environmental variables
ENV LD_PRELOAD="/usr/lib/libjemalloc.so.2"
ENV UV_SYSTEM_PYTHON="1"

# Set some labels
LABEL \
    org.opencontainers.image.title="Music Assistant Server" \
    org.opencontainers.image.description="Music Assistant Server/Core" \
    org.opencontainers.image.source="https://github.com/music-assistant/server" \
    org.opencontainers.image.authors="The Music Assistant Team" \
    org.opencontainers.image.documentation="https://github.com/orgs/music-assistant/discussions" \
    org.opencontainers.image.licenses="Apache License 2.0" \
    io.hass.version="${MASS_VERSION}" \
    io.hass.type="addon" \
    io.hass.name="Music Assistant Server" \
    io.hass.description="Music Assistant Server/Core" \
    io.hass.platform="${TARGETPLATFORM}" \
    io.hass.type="addon"

VOLUME [ "/data" ]
EXPOSE 8095

ENTRYPOINT ["mass", "--config", "/data"]
