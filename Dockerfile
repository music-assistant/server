FROM alpine:3.12

ARG JEMALLOC_VERSION=5.2.1
WORKDIR /usr/src/
COPY . .

# Install packages
RUN set -x \
    && apk update \
    && echo "http://dl-8.alpinelinux.org/alpine/edge/community" >> /etc/apk/repositories \
    && echo "http://dl-8.alpinelinux.org/alpine/edge/testing" >> /etc/apk/repositories \
    # install default packages
    && apk add --no-cache \
        tzdata \
        ca-certificates \
        curl \
        flac \
        sox \
        libuv \
        ffmpeg \
        python3 \
        py3-pillow \
        py3-numpy \
        py3-scipy \
        py3-aiohttp \
        py3-jwt \
        py3-passlib \
        py3-cryptography \
        py3-zeroconf \
        py3-pytaglib \
        py3-pip \
    # install (temp) build packages
    && apk add --no-cache --virtual .build-deps \
        build-base \
        python3-dev \
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
        zlib-dev \
        libuv-dev \
        libffi \
    # setup jmalloc
    && curl -L -f -s "https://github.com/jemalloc/jemalloc/releases/download/${JEMALLOC_VERSION}/jemalloc-${JEMALLOC_VERSION}.tar.bz2" \
            | tar -xjf - -C /usr/src \
        && cd /usr/src/jemalloc-${JEMALLOC_VERSION} \
        && ./configure \
        && make \
        && make install \
        && cd /usr/src \
    # make sure uvloop is installed
    &&  pip install uvloop \
    # install music assistant
    && python3 setup.py install \
    # cleanup build files
    && apk del .build-deps \
        && rm -rf /usr/src/*

ENV DEBUG=false
EXPOSE 8095/tcp

VOLUME [ "/data" ]

ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so
ENTRYPOINT ["python3", "-m", "music_assistant", "--config", "/data"]