# syntax=docker/dockerfile:1
ARG TARGETPLATFORM="linux/amd64"
ARG BUILD_VERSION=latest
ARG PYTHON_VERSION="3.11"

#####################################################################
#                                                                   #
# Build Wheels                                                      #
#                                                                   #
#####################################################################
FROM python:${PYTHON_VERSION}-slim as wheels-builder
ARG TARGETPLATFORM

# Install buildtime packages
RUN set -x \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        cargo \
        git

WORKDIR /wheels
COPY requirements_all.txt .


# build python wheels for all dependencies
RUN set -x \
    && pip install --upgrade pip \
    && pip install build maturin \
    && pip wheel -r requirements_all.txt

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
FROM python:${PYTHON_VERSION}-slim AS final-build
WORKDIR /app

RUN set -x \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        wget \
        tzdata \
        ffmpeg \
        libsox-fmt-all \
        libsox3 \
        sox \
        cifs-utils \
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
ARG TARGETPLATFORM

# Set some labels for the Home Assistant add-on
LABEL \
    io.hass.version=${BUILD_VERSION} \
    io.hass.name="Music Assistant" \
    io.hass.description="Music Assistant Server/Core" \
    io.hass.platform="${TARGETPLATFORM}" \
    io.hass.type="addon"

VOLUME [ "/data" ]

ENTRYPOINT ["mass", "--config", "/data"]
