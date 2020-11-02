FROM python:3.8-alpine3.12

# Versions
ARG JEMALLOC_VERSION=5.2.1
ARG MASS_VERSION=0.0.60

# Base system
WORKDIR /tmp

# Install packages
RUN set -x \
    && apk update \
    && echo "http://dl-8.alpinelinux.org/alpine/edge/community" >> /etc/apk/repositories \
    # default packages
    && apk add --no-cache \
        tzdata \
        ca-certificates \
        curl \
        bind-tools \
        flac \
        sox \
        ffmpeg \
        libsndfile \
        taglib \
        openblas \
        libgfortran \
        lapack \
    # build packages
    && apk add --no-cache --virtual .build-deps \
        build-base \
        libsndfile-dev \
        taglib-dev \
        openblas-dev \
        lapack-dev \
        libffi-dev \
        gcc \
        gfortran \
        freetype-dev \
        libpng-dev \
        libressl-dev \
        fribidi-dev \
        harfbuzz-dev \
        jpeg-dev \
        lcms2-dev \
        openjpeg-dev \
        tcl-dev \
        tiff-dev \
        tk-dev \
        zlib-dev

# setup jmalloc
RUN curl -L -f -s "https://github.com/jemalloc/jemalloc/releases/download/${JEMALLOC_VERSION}/jemalloc-${JEMALLOC_VERSION}.tar.bz2" \
        | tar -xjf - -C /usr/src \
    && cd /usr/src/jemalloc-${JEMALLOC_VERSION} \
    && ./configure \
    && make \
    && make install \
    && rm -rf /usr/src/jemalloc-${JEMALLOC_VERSION} \
    # change workdir back to /tmp
    && cd /tmp

# build orjson
ENV RUSTFLAGS "-C target-feature=-crt-static"
RUN wget -O rustup.sh https://sh.rustup.rs \
    && sh rustup.sh -y \
    && cp $HOME/.cargo/bin/* /usr/local/bin \
    && rustup install nightly \
    && rustup default nightly \
    && pip install orjson \
    && rustup self uninstall -y \
    && rm rustup.sh

# install uvloop and music assistant
RUN pip install --upgrade uvloop music-assistant==${MASS_VERSION}

# cleanup build files
RUN apk del .build-deps \
    && rm -rf /usr/src/*


ENV DEBUG=false
VOLUME [ "/data" ]

ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so
ENTRYPOINT ["python", "-m", "music_assistant", "--config", "/data"]