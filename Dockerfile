FROM python:3.11.2

# Install component packages
RUN \
    set -x \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libuv1 \
        curl \
        wget \
        ffmpeg \
        git \
        libjpeg62-turbo

COPY . ./

# Install mass wheel and dependencies
RUN pip3 install --no-cache-dir .[server]


EXPOSE 8095/tcp
EXPOSE 9090/tcp
EXPOSE 3483/tcp

VOLUME [ "/data" ]

ENTRYPOINT ["mass", "--config", "/data"]
