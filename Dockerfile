FROM python:3.8-slim

# Build arguments
# ARG BUILD_ARCH="amd64"
ARG MASS_VERSION=0.0.60
ARG JEMALLOC_VERSION=5.2.1
ARG S6_OVERLAY_VERSION=2.1.0.2


RUN BUILD_ARCH="$(uname -m)" && \
    apt-get update && apt-get install -y --no-install-recommends \
		# required packages
		git bash jq flac sox libsox-fmt-mp3 zip curl unzip ffmpeg libsndfile1 libtag1v5 libblas3 liblapack3 \
		# build packages
		wget libtag1-dev build-essential liblapack-dev libblas-dev gfortran libatlas-base-dev && \
    # Setup jemalloc
    curl -L -s https://github.com/jemalloc/jemalloc/releases/download/${JEMALLOC_VERSION}/jemalloc-${JEMALLOC_VERSION}.tar.bz2 | tar -xjf - -C /usr/src && \
    cd /usr/src/jemalloc-${JEMALLOC_VERSION} && \
    ./configure && \
    make && \
    make install && \
    rm -rf /usr/src/jemalloc-${JEMALLOC_VERSION} && \
    # Setup s6 overlay
    curl -L -s "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${BUILD_ARCH}.tar.gz" | tar zxvf - -C / && \
    mkdir -p /etc/fix-attrs.d && \
    mkdir -p /etc/services.d && \
    cd /tmp && \
    # make sure uvloop is installed
    pip install --upgrade uvloop && \
    # install music assistant
    cd /tmp && pip install --upgrade music-assistant==${MASS_VERSION} && \
	# cleanup build packages
	apt-get purge -y --auto-remove libtag1-dev build-essential liblapack-dev libblas-dev gfortran libatlas-base-dev && \
	rm -rf /var/lib/apt/lists/*

# copy rootfs
COPY rootfs /

ENV DEBUG=False
VOLUME [ "/data" ]

ENTRYPOINT ["/init"]