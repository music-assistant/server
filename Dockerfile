# syntax=docker/dockerfile:experimental
ARG HASS_ARCH=amd64
ARG BUILD_VERSION=latest
ARG PYTHON_VERSION="3.11"

#####################################################################
#                                                                   #
# Build Wheels                                                      #
#                                                                   #
#####################################################################
FROM python:${PYTHON_VERSION}-alpine as wheels-builder
ARG HASS_ARCH

ENV PATH="${PATH}:/root/.cargo/bin"

# Install buildtime packages
RUN set -x \
    && apk add --no-cache \
        alpine-sdk \
        patchelf \
        build-base \
        cmake \
        git \
        linux-headers \
        autoconf \
        automake \
        cargo \
        libffi \
        libffi-dev \
        git

WORKDIR /wheels
COPY requirements_all.txt .

# build python wheels for all dependencies
RUN set -x \
    && pip install --upgrade pip \
    && pip install build \
    && pip wheel -r requirements_all.txt --find-links "https://wheels.home-assistant.io/musllinux/"

# build music assistant wheel
COPY music_assistant music_assistant
COPY pyproject.toml .
COPY MANIFEST.in .
RUN python3 -m build --wheel --outdir /wheels --skip-dependency-check

#####################################################################
#                                                                   #
# Final Image                                                       #
#                                                                   #
#####################################################################
FROM python:${PYTHON_VERSION}-alpine AS final-build
WORKDIR /app

RUN set -x \
    && apk add --no-cache \
        ca-certificates \
        curl \
        git \
        jq \
        openssl \
        tzdata \
        ffmpeg \
        ffmpeg-libs \
        libjpeg-turbo \
    # cleanup
    && rm -rf /tmp/* \
    && rm -rf /var/lib/apt/lists/*


# https://github.com/moby/buildkit/blob/master/frontend/dockerfile/docs/syntax.md#build-mounts-run---mount
# Install all built wheels
RUN --mount=type=bind,target=/tmp/wheels,source=/wheels,from=wheels-builder,rw \
    set -x \
    && pip install --upgrade pip \
    && pip install --no-cache-dir /tmp/wheels/*.whl

# Required to persist build arg
ARG BUILD_VERSION
ARG HASS_ARCH

# Set some labels for the Home Assistant add-on
LABEL \
    io.hass.version=${BUILD_VERSION} \
    io.hass.name="Music Assistant" \
    io.hass.description="Music Assistant Server/Core" \
    io.hass.arch="${HASS_ARCH}" \
    io.hass.type="addon"

EXPOSE 8095/tcp
EXPOSE 9090/tcp
EXPOSE 3483/tcp

VOLUME [ "/data" ]

ENTRYPOINT ["mass", "--config", "/data"]
