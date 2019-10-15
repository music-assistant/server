FROM python:3.7-buster

RUN apt-get update && apt-get install -y --no-install-recommends \
		flac sox zip curl wget ffmpeg libsndfile1 libtag1-dev build-essential \
        python3-numpy python3-scipy python3-matplotlib python3-taglib \
	&& rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

# copy app files
RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app
COPY music_assistant /usr/src/app/music_assistant
COPY mass.py /usr/src/app/mass.py
RUN chmod a+x /usr/src/app/mass.py

VOLUME ["/data"]

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV mass_debug false
ENV mass_datadir /data
ENV mass_update false

CMD ["python3 /usr/src/app/mass.py"]