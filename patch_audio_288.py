"""Patch the DASH duration cap into MA's audio.py on the container."""
import re

path = "/app/venv/lib/python3.13/site-packages/music_assistant/controllers/streams/audio.py"
with open(path) as f:
    content = f.read()

old = """                yield chunk
                del chunk
            # if we received no audio"""

new = """                yield chunk
                del chunk
                # DASH streams (e.g. Tidal) never end - cap at duration + 5s
                if (
                    streamdetails.duration
                    and (bytes_received / pcm_format.pcm_sample_size + seek_position)
                    >= streamdetails.duration + 5
                ):
                    break
            # if we received no audio"""

assert old in content, "Pattern not found in audio.py!"
content = content.replace(old, new, 1)

with open(path, "w") as f:
    f.write(content)

print("OK - patched audio.py with DASH duration cap")