FROM python:3.7.3-alpine

# install deps
RUN apk add build-base python-dev flac sox taglib-dev
COPY requirements.txt requirements.txt
RUN pip install --upgrade -r requirements.txt

# copy files
RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app
COPY music_assistant /usr/src/app
RUN chmod a+x /usr/src/app/main.py

VOLUME ["/data"]

CMD ["python3.7", "/usr/src/app/main.py", "/data"]