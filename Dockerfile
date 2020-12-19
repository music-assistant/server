FROM python:3.8-slim as builder

ENV PIP_EXTRA_INDEX_URL=https://www.piwheels.org/simple

RUN set -x \
    # Install buildtime packages
    && apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        build-essential \
        gcc \
        libtag1-dev \
        libffi-dev \
        libssl-dev \
        zlib1g-dev \
        xvfb \
        tcl8.6-dev \
        tk8.6-dev \
        libjpeg-turbo-progs \
        libjpeg62-turbo-dev

# build jemalloc
ARG JEMALLOC_VERSION=5.2.1
RUN curl -L -s https://github.com/jemalloc/jemalloc/releases/download/${JEMALLOC_VERSION}/jemalloc-${JEMALLOC_VERSION}.tar.bz2 \
        | tar -xjf - -C /tmp \
    && cd /tmp/jemalloc-${JEMALLOC_VERSION} \
    && ./configure \
    && make \
    && make install

# build python wheels
WORKDIR /wheels
COPY . /tmp
RUN pip wheel uvloop cchardet aiodns brotlipy \
    && pip wheel -r /tmp/requirements.txt \
    # Include frontend-app in the source files
    && curl -L https://github.com/music-assistant/app/archive/master.tar.gz | tar xz \
    && mv app-master/docs /tmp/music_assistant/web/static \
    && pip wheel /tmp
    
#### FINAL IMAGE
FROM python:3.8-slim AS final-image

WORKDIR /wheels
COPY --from=builder /wheels /wheels
COPY --from=builder /usr/local/lib/libjemalloc.so /usr/local/lib/libjemalloc.so
RUN set -x \
    # Install runtime dependency packages
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        tzdata \
        ca-certificates \
        flac \
        sox \
        libsox-fmt-all \
        ffmpeg \
        libtag1v5 \
        openssl \
        libjpeg62-turbo \
        zlib1g \
    # install music assistant (and all it's dependencies) using the prebuilt wheels
    && pip install --no-cache-dir -f /wheels music_assistant \
    # cleanup
    && rm -rf /tmp/* \
    && rm -rf /wheels \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /root/*

ENV DEBUG=false
EXPOSE 8095/tcp

VOLUME [ "/data" ]

ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so
ENTRYPOINT ["mass", "--config", "/data"]