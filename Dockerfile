FROM alpine:3.12

# Versions
ARG JEMALLOC_VERSION=5.2.1
ARG MASS_VERSION=0.0.62

# Install packages
RUN set -x \
    && apk update \
    && echo "http://dl-8.alpinelinux.org/alpine/edge/community" >> /etc/apk/repositories \
    && echo "http://dl-8.alpinelinux.org/alpine/edge/testing" >> /etc/apk/repositories \
    # default packages
    && apk add --no-cache \
        tzdata \
        ca-certificates \
        curl \
        bind-tools \
        flac \
        sox \
        ffmpeg \
        python3 \
        py3-numpy \
        py3-scipy \
        py3-pytaglib \
        py3-pillow \
        py3-pip \
    # build packages
    && apk add --no-cache --virtual .build-deps \
        build-base

# setup jmalloc
RUN mkdir /usr/src \
    && curl -L -f -s "https://github.com/jemalloc/jemalloc/releases/download/${JEMALLOC_VERSION}/jemalloc-${JEMALLOC_VERSION}.tar.bz2" \
        | tar -xjf - -C /usr/src \
    && cd /usr/src/jemalloc-${JEMALLOC_VERSION} \
    && ./configure \
    && make \
    && make install \
    && rm -rf /usr/src/jemalloc-${JEMALLOC_VERSION} \
    # change workdir back to /tmp
    && cd /tmp

# install uvloop and music assistant
RUN pip install --upgrade uvloop music-assistant==${MASS_VERSION}

# cleanup build files
RUN apk del .build-deps \
    && rm -rf /usr/src/*

ENV DEBUG=false
VOLUME [ "/data" ]

ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so
ENTRYPOINT ["python", "-m", "music_assistant", "--config", "/data"]