FROM python:3.8.0rc1-alpine3.10

# install deps
RUN apk add flac sox zip curl wget ffmpeg taglib
# RUN apk add --no-cache -X http://dl-cdn.alpinelinux.org/alpine/edge/testing py3-numpy py3-scipy py3-matplotlib py3-aiohttp py3-cairocffi
COPY requirements.txt requirements.txt
RUN apk --no-cache add --virtual .builddeps build-base taglib-dev && \
    python3 -m pip install -r requirements.txt && \
    apk del .builddeps && \
    rm -rf /root/.cache

# copy files
RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app
COPY music_assistant /usr/src/app/music_assistant
COPY main.py /usr/src/app/main.py
RUN chmod a+x /usr/src/app/main.py

VOLUME ["/data"]

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV autoupdate false

CMD ["/run.sh"]