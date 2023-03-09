# syntax=docker/dockerfile:1
ARG TARGETPLATFORM="linux/amd64"
ARG BUILD_VERSION=latest
ARG PYTHON_VERSION="3.11"

#####################################################################
#                                                                   #
# Build Wheels                                                      #
#                                                                   #
#####################################################################
FROM python:${PYTHON_VERSION}-alpine3.16 as wheels-builder
ARG TARGETPLATFORM

# Install buildtime packages
RUN set -x \
    && apk add --no-cache \
        alpine-sdk \
        ca-certificates \
        openssh-client \
        patchelf \
        build-base \
        cmake \
        git \
        gcc \
        g++ \
        musl-dev \
        linux-headers \
        autoconf \
        automake \
        libffi \
        libffi-dev \
        openssl-dev \
        pkgconfig

WORKDIR /wheels
COPY requirements_all.txt .


ENV PATH="/root/.cargo/bin:${PATH}"

# build python wheels for all dependencies
RUN set -x \
    && pip install --upgrade pip \
    && pip install build maturin \
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
FROM python:${PYTHON_VERSION}-alpine3.16 AS final-build
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
ARG TARGETPLATFORM

# Set some labels for the Home Assistant add-on
LABEL \
    io.hass.version=${BUILD_VERSION} \
    io.hass.name="Music Assistant" \
    io.hass.description="Music Assistant Server/Core" \
    io.hass.platform="${TARGETPLATFORM}" \
    io.hass.type="addon"

EXPOSE 8095/tcp
EXPOSE 9090/tcp
EXPOSE 3483/tcp

VOLUME [ "/data" ]

ENTRYPOINT ["mass", "--config", "/data"]
