FROM python:3.11.2-alpine3.16

# Add Home Assistant wheels repository
ENV WHEELS_LINKS=https://wheels.home-assistant.io/musllinux/

# Install component packages
RUN \
    apk add --no-cache \
    curl \
    ffmpeg \
    ffmpeg-libs \
    git \
    libjpeg-turbo \
    mariadb-connector-c

COPY . ./

# Install mass wheel and dependencies
RUN pip3 install --no-cache-dir --find-links ${WHEELS_LINKS} \
    .[server]


EXPOSE 8095/tcp
EXPOSE 9090/tcp
EXPOSE 3483/tcp

VOLUME [ "/data" ]

ENTRYPOINT ["mass", "--config", "/data"]
