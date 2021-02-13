FROM musicassistant/base-image

# install latest version
COPY . /tmp
RUN cd /tmp \
    # Include frontend-app in the source files
    && curl -L https://github.com/music-assistant/app/archive/master.tar.gz | tar xz \
    && mv app-master/docs /tmp/music_assistant/web/static \
    && pip install --no-cache-dir music_assistant \
    # cleanup
    && rm -rf /tmp/*

EXPOSE 8095/tcp

VOLUME [ "/data" ]

ENTRYPOINT ["mass", "--config", "/data"]