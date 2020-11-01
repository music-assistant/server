FROM python:3.8-slim

# Build arguments
ARG MASS_VERSION=0.0.60
ARG JEMALLOC_VERSION=5.2.1

ARG TARGETPLATFORM
ARG BUILDPLATFORM

WORKDIR /usr/src

RUN set -x \
    && apt-get update && apt-get install -y --no-install-recommends \
        # required packages
		    git bash jq tzdata curl ca-certificates flac sox libsox-fmt-mp3 zip curl unzip ffmpeg libsndfile1 libtag1v5 libblas3 liblapack3 \
        # build packages
        libtag1-dev build-essential liblapack-dev libblas-dev gfortran libatlas-base-dev \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/share/man/man1 \
    \
    # Setup jemalloc
    && curl -L -s https://github.com/jemalloc/jemalloc/releases/download/${JEMALLOC_VERSION}/jemalloc-${JEMALLOC_VERSION}.tar.bz2 | tar -xjf - -C /usr/src \
    && cd /usr/src/jemalloc-${JEMALLOC_VERSION} \
    && ./configure \
    && make \
    && make install \
    && rm -rf /usr/src/jemalloc-${JEMALLOC_VERSION} \
    \
    && cd /tmp \
    \
    # rustup requirement for maturin/orjson
    # && pip install maturin \
    # && curl https://sh.rustup.rs -sSf | sh -s -- --default-toolchain nightly-2020-10-24 --profile minimal -y \
    # && source $HOME/.cargo/env \
    # install uvloop and music assistant
    && pip install --upgrade uvloop music-assistant==${MASS_VERSION} \
    # cleanup build packages
    && apt-get purge -y --auto-remove libtag1-dev build-essential liblapack-dev libblas-dev gfortran libatlas-base-dev \
    && rm -rf /var/lib/apt/lists/*


ENV DEBUG=false
VOLUME [ "/data" ]

ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so
ENTRYPOINT ["python", "-m", "music_assistant", "--config", "/data"]