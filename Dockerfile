FROM python:3.7-slim

COPY requirements.txt requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
		# required packages
		flac sox libsox-fmt-mp3 zip curl wget unzip ffmpeg libsndfile1 libtag1v5 \
		# build packages
		libtag1-dev build-essential && \
	# install required python packages with pip
	pip install -r requirements.txt && \
	# cleanup build packages
	apt-get purge -y --auto-remove libtag1-dev build-essential && \
	rm -rf /var/lib/apt/lists/*

# copy app files
RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app
COPY music_assistant /usr/src/app/music_assistant
COPY mass.py /usr/src/app/mass.py
RUN chmod a+x /usr/src/app/mass.py

VOLUME ["/data"]

ENV mass_debug false
ENV mass_datadir /data
ENV mass_update false

CMD ["/usr/src/app/mass.py"]