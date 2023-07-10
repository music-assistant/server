# syntax=docker/dockerfile:1
ARG BUILD_ARCH
ARG BASE_IMAGE_VERSION="3.11-alpine3.18"

FROM ghcr.io/home-assistant/$BUILD_ARCH-base-python:${BASE_IMAGE_VERSION}

ARG MASS_VERSION
ENV S6_SERVICES_GRACETIME=220000
ENV WHEELS_LINKS="https://wheels.home-assistant.io/musllinux/"

WORKDIR /usr/src

# Install OS requirements
RUN apk update \
    && apk add --no-cache \
        git \
        wget \
        ffmpeg \
        sox \
        cifs-utils \
        nfs-utils

## Setup Core dependencies
COPY requirements_all.txt .
RUN pip3 install \
    --no-cache-dir \
    --only-binary=:all: \
    --find-links ${WHEELS_LINKS} \
    -r requirements_all.txt

# Install Music Assistant
RUN pip3 install \
        --no-cache-dir \
        music-assistant[server]==${MASS_VERSION} \
    && python3 -m compileall music-assistant

# Set some labels
LABEL \
    org.opencontainers.image.title="Music Assistant" \
    org.opencontainers.image.description="Music Assistant Server/Core" \
    org.opencontainers.image.source="https://github.com/music-assistant/server" \
    org.opencontainers.image.authors="The Music Assistant Team" \
    org.opencontainers.image.documentation="https://github.com/orgs/music-assistant/discussions" \
    org.opencontainers.image.licenses="Apache License 2.0" \
    io.hass.version=${MASS_VERSION} \
    io.hass.type="addon" \
    io.hass.name="Music Assistant" \
    io.hass.description="Music Assistant Server/Core" \
    io.hass.platform="linux/${BUILD_ARCH}" \
    io.hass.type="addon"

VOLUME [ "/data" ]

# S6-Overlay
COPY rootfs /

WORKDIR /data
