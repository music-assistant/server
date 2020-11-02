FROM python:3.8-slim

# Versions
ARG JEMALLOC_VERSION=5.2.1
ARG MASS_VERSION=0.0.61

# Base system
WORKDIR /tmp

# Install packages
RUN set -x \
    && apt-get update && apt-get install -y --no-install-recommends \
        # required packages
		    git jq tzdata curl ca-certificates flac sox libsox-fmt-all zip curl ffmpeg libsndfile1 libtag1v5 \
        # build packages
        # build-essential libtag1-dev libffi-dev\
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/share/man/man1

# setup jmalloc
RUN curl -L -s https://github.com/jemalloc/jemalloc/releases/download/${JEMALLOC_VERSION}/jemalloc-${JEMALLOC_VERSION}.tar.bz2 | tar -xjf - -C /usr/src \
    && cd /usr/src/jemalloc-${JEMALLOC_VERSION} \
    && ./configure \
    && make \
    && make install \
    && rm -rf /usr/src/jemalloc-${JEMALLOC_VERSION} \
    \
    && cd /tmp

# install uvloop and music assistant
RUN pip install --upgrade uvloop music-assistant==${MASS_VERSION}

# cleanup build files
RUN apt-get purge -y --auto-remove libtag1-dev libffi-dev build-essential \
    && rm -rf /var/lib/apt/lists/*


ENV DEBUG=false
VOLUME [ "/data" ]

ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so
ENTRYPOINT ["python", "-m", "music_assistant", "--config", "/data"]