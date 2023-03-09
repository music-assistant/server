# syntax=docker/dockerfile:experimental
ARG HASS_ARCH=amd64
ARG BUILD_VERSION=latest
ARG PYTHON_VERSION="3.11"

#####################################################################
#                                                                   #
# Build Wheels                                                      #
#                                                                   #
#####################################################################
FROM python:${PYTHON_VERSION}-slim as wheels-builder
ARG HASS_ARCH

ENV PIP_EXTRA_INDEX_URL=https://www.piwheels.org/simple
ENV PATH="${PATH}:/root/.cargo/bin"

# Install buildtime packages
RUN set -x \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        gcc \
        git \
        libffi-dev \
        libssl-dev

RUN set -x \
    \
    && if [ ${HASS_ARCH} = "amd64" ]; then RUST_ARCH="x86_64-unknown-linux-gnu"; fi \
    && if [ ${HASS_ARCH} = "armv7" ]; then RUST_ARCH="armv7-unknown-linux-gnueabihf"; fi \
    && if [ ${HASS_ARCH} = "aarch64" ]; then RUST_ARCH="aarch64-unknown-linux-gnu"; fi \
    \
    && curl -o rustup-init https://static.rust-lang.org/rustup/dist/${RUST_ARCH}/rustup-init \
    && chmod +x rustup-init \
    && ./rustup-init -y --no-modify-path --profile minimal --default-host ${RUST_ARCH}

WORKDIR /wheels
COPY requirements.txt .

# build python wheels
RUN set -x \
    && pip wheel -r .[server]


#####################################################################
#                                                                   #
# Final Image                                                       #
#                                                                   #
#####################################################################
FROM python:${PYTHON_VERSION}-slim AS final-build
WORKDIR /app

ENV DEBIAN_FRONTEND="noninteractive"

RUN set -x \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
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
    && pip install --no-cache-dir -f /tmp/wheels -r /tmp/wheels/requirements.txt

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
