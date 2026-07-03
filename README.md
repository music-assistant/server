Music Assistant
==================================

**Music Assistant Server**

Music Assistant is a free, opensource Media library manager that connects to your streaming services and a wide range of connected speakers. The server is the beating heart, the core of Music Assistant and must run on an always-on device like a Raspberry Pi, a NAS or an Intel NUC or alike.

**Documentation and support**

Documentation https://music-assistant.io

Beta Documentation https://beta.music-assistant.io

For issues, please go to [the issue tracker](https://github.com/music-assistant/support/issues).

For feature requests, please see [feature requests](https://github.com/music-assistant/support/discussions/categories/feature-requests-and-ideas).

____________


## Running the server

Music Assistant can be operated as a complete standalone product but it is actually tailored to use side by side with Home Assistant, it is meant with automation in mind, hence our recommended installation method is to run the server as a Home Assistant app.


### Supported installation methods

The only officially supported ways to run the Music Assistant server are:

- **Home Assistant app** (recommended) — see https://music-assistant.io/installation/
- **Docker container** — `ghcr.io/music-assistant/server`

Both bundle every system dependency the server needs. Although Music Assistant's main code is Python, it depends on external/OS components — a recent **ffmpeg (6.1+)** with a specific codec set, native libraries (e.g. jemalloc and CIFS/NFS client libraries) and a few bundled binaries — which a plain PyPI/`pip` install can't provide. The server is therefore **not published to PyPI**; run it via the app or container above.

[repository-badge]: https://img.shields.io/badge/Add%20repository%20to%20my-Home%20Assistant-41BDF5?logo=home-assistant&style=for-the-badge
[repository-url]: https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmusic-assistant%2Fhome-assistant-addon

### Running from source (development)

For local development you provide the system dependencies yourself — **Python 3.14+** and **ffmpeg 6.1+** are required.

- `scripts/setup.sh` — create the virtualenv and install dependencies and pre-commit hooks
- `python -m music_assistant --log-level debug` — run the server locally (listens on http://localhost:8095)
- `pytest` runs the tests; `pre-commit run --all-files` runs the linters

---

[![A project from the Open Home Foundation](https://www.openhomefoundation.org/badges/ohf-project.png)](https://www.openhomefoundation.org/)
