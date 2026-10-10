# Music Assistant dev image: the published nightly (built from dev) with this checkout's
# music_assistant package laid over it, so no wheel or compiler is needed to try a branch.
ARG MA_IMAGE=ghcr.io/music-assistant/server:nightly
FROM ${MA_IMAGE}

COPY music_assistant /tmp/music_assistant
RUN SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
    && cp -a /tmp/music_assistant/. "${SITE_PACKAGES}/music_assistant/" \
    && rm -rf /tmp/music_assistant \
    && python -m compileall -q "${SITE_PACKAGES}/music_assistant"
