FROM python:3.8-alpine3.12 AS builder

#### BUILD DEPENDENCIES AND PYTHON WHEELS
RUN echo "http://dl-8.alpinelinux.org/alpine/edge/community" >> /etc/apk/repositories \
    && apk update \
    && apk add  \
        curl \
        bind-tools \
        ca-certificates \
        alpine-sdk \
        build-base \
        openblas-dev \
        lapack-dev \
        libffi-dev \
        python3-dev \
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
        libffi-dev \
        # pillow deps ?
        freetype \
        lcms2 \
        libimagequant \
        libjpeg-turbo \
        libwebp \
        libxcb \
        openjpeg \
        tiff \
        zlib \
        taglib-dev \
        libsndfile-dev

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
COPY ./requirements.txt /wheels/requirements.txt
RUN pip install -U pip \
    && pip wheel -r ./requirements.txt \
    && pip wheel uvloop

#### FINAL IMAGE
FROM python:3.8-alpine3.12

WORKDIR /usr/src/
COPY . .
COPY --from=builder /wheels /wheels
COPY --from=builder /usr/local/lib/libjemalloc.so /usr/local/lib/libjemalloc.so
RUN set -x \
    # Install runtime dependency packages
    && apk add --no-cache --upgrade --repository http://dl-cdn.alpinelinux.org/alpine/edge/main \
        libgcc \
        tzdata \
        ca-certificates \
        bind-tools \
        curl \
        flac \
    && apk add --no-cache --upgrade --repository http://dl-cdn.alpinelinux.org/alpine/edge/community \
        sox \
        ffmpeg \
        taglib \
        libsndfile \
    # Make sure pip is updated
    pip install -U pip \
    # make sure uvloop is installed
    && pip install --no-cache-dir uvloop -f /wheels \
    # pre-install all requirements (needed for numpy/scipy)
    && pip install --no-cache-dir -r /wheels/requirements.txt -f /wheels \
    # Include frontend-app in the source files
    && curl -L https://github.com/music-assistant/app/archive/master.tar.gz | tar xz \
    && mv app-master/docs /usr/src/music_assistant/web/static \
    # install music assistant
    && python3 setup.py install \
    # cleanup
    && rm -rf /usr/src/* \
    && rm -rf /tmp/* \
    && rm -rf /wheels

ENV DEBUG=false
EXPOSE 8095/tcp

VOLUME [ "/data" ]

ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so
ENTRYPOINT ["mass", "--config", "/data"]