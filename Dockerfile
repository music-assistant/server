# syntax=docker/dockerfile:1

FROM ghcr.io/music-assistant/base:latest

ARG MASS_VERSION
ARG TARGETPLATFORM

# Install Music Assistant from published wheel on PyPi
RUN uv pip install \
    --system \
    --no-cache \
    --find-links "https://wheels.home-assistant.io/musllinux/" \
    dist/music_assistant-${MASS_VERSION}-py3-none-any.whl

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
