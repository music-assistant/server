## Installation (Docker)

1. Locate the `providers` folder inside your Music Assistant container, eg currently it's:

   `/app/venv/lib/python3.13/site-packages/music_assistant/providers`

2. Copy the `sverigesradio` folder from this repo into that `providers` directory, so you end up with:

   `/app/venv/lib/python3.13/site-packages/music_assistant/providers/sverigesradio/__init__.py`  
   `/app/venv/lib/python3.13/site-packages/music_assistant/providers/sverigesradio/manifest.json`

   Example (run on host):

      ```bash
   docker cp sverigesradio musicassistant:/app/venv/lib/python3.13/site-packages/music_assistant/providers/sverigesradio