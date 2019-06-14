FROM python:3.7.3-alpine

# install deps
RUN apk add build-base python-dev flac sox taglib-dev zip curl ffmpeg ffmpeg-dev sox-dev py-numpy
COPY requirements.txt requirements.txt
RUN pip install --upgrade -r requirements.txt

# copy files
RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app
COPY music_assistant /usr/src/app
RUN chmod a+x /usr/src/app/main.py

VOLUME ["/data"]

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV autoupdate false

CMD ["/run.sh"]