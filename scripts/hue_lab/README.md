# Hue lab

Tune Hue light effects at your desk instead of in a dark room with the bridge.

The loop is: record the visualizer features Music Assistant sends to the Hue
provider while real music plays, replay them offline through any effects engine,
and watch the result in a browser preview — optionally with the song playing.
Recordings hold extracted features only (spectrum, onsets, beats, palette), never audio.

## 1. Record

Start the server with the recorder switched on, then play music to a group that
includes a Hue light player:

```sh
MASS_HUE_RECORD_DIR=~/hue-recordings python -m music_assistant --log-level debug
```

Each light session (lights come up → go down) writes one
`<date>-<area>.jsonl` file. Without the variable the recorder is fully off.

No bridge handy? Generate a synthetic track with a known intro/build/drop layout:

```sh
python -m scripts.hue_lab synth -o synth.jsonl --bpm 124
```

## 2. Render

Replay a recording through an engine exactly as the bridge's 30 Hz render loop
would — events are delivered by arrival time, frames rendered at `now + latency`:

```sh
python -m scripts.hue_lab render ~/hue-recordings/20261028-201500-playroom.jsonl --mode energetic
```

`--engine path/to/file.py:ClassName` swaps in a different engine. Any class with the
same interface as `HueAudioAnalyzer` works (see `Engine` in `replay.py`), which is
how the new effects engine gets compared against the current one on identical input.
The lab only needs `pip install hue-entertainment`, not the whole server.

## 3. Preview

```sh
cd scripts/hue_lab/preview
npm install
npm run dev
```

Open the page, drop in the `.frames.json` file, and optionally add the same song.
Space plays and pauses, arrows step a second (Shift for five), `[` and `]` nudge the
song offset by 10 ms. The bottom strip shows every light's color across the whole
track, so you can see whether effects follow the song's structure at a glance.
