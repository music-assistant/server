# Spotify Connect Provider - Architecture

## Overview

The Spotify Connect provider makes any Music Assistant player (or sync group) appear as a
device in the official Spotify app via Spotify's Connect protocol. Selecting that device in
the Spotify app streams audio into Music Assistant; the player's transport and volume
controls are proxied back to Spotify.

The provider wraps **go-librespot** ([devgianlu/go-librespot](https://github.com/devgianlu/go-librespot)),
a reverse-engineered Spotify Connect client, running as a subprocess and driven entirely over
its local HTTP + WebSocket API. Music Assistant needs **no** Spotify Web API credentials and
**no** configured Spotify *music* provider to control playback.

## How it works

```
   Spotify app  (mobile / desktop / web)
        │   Connect protocol: mDNS discovery + audio
        ▼
   go-librespot daemon  (one instance per player)
        │   decodes Ogg Vorbis → s16le PCM on stdout, and is driven by
        │   GoLibrespotClient over its local HTTP + WS API:
        │   REST control (resume/pause/seek/volume) + /events (state, metadata)
        ▼
   get_audio_stream  (CUSTOM stream: source-paced, ends on pause)
        │
        ▼
   MA streams controller  (ffmpeg resample, per player)
        │
        ▼
   Music Assistant player
```

### Key components

- **go-librespot subprocess** (`_daemon_runner`): launched with `--config_dir <cache_dir>`;
  decodes the Ogg Vorbis stream and writes raw PCM to its **stdout** (`audio_backend: pipe`,
  `audio_output_pipe: /dev/stdout`). Exposes a loopback HTTP + WebSocket API on a free port,
  one per instance. Supervised — restarts on exit, `unload_with_error`s after repeated failures.
- **`GoLibrespotClient`** (`client.py`): REST control (`POST /player/{resume,pause,next,prev,
  seek,volume,play}`) plus the `/events` WebSocket, which pushes `{"type": ..., "data": ...}`
  messages for session/playback/metadata/volume state. `204 No Content` means "no active
  session" and is treated as a no-op.
- **AudioSource MediaItem**: a single live item browsable under the global "Live Inputs" node,
  played through the standard `play_media` flow (like a radio station). `exclusive=True`,
  `allow_external_trigger=True`. Transport capabilities are statically enabled — go-librespot's
  REST API always provides them while a session is active.
- **Stream metadata**: live track info (title/artist/album/artwork/elapsed) is pushed to the
  active queue item's `StreamDetails.stream_metadata` from the WebSocket `metadata`/`seek`
  events — the same channel ICY radio metadata uses.

## Audio transport

```
go-librespot ──s16le PCM──▶ stdout ──▶ get_audio_stream ──▶ ffmpeg (resample) ──▶ player
```

- **`StreamType.CUSTOM`**: `get_audio_stream` reads the daemon's stdout and yields PCM. The
  subprocess pipe always has a reader, so go-librespot's non-blocking pipe open never fails,
  and we control the byte stream (pacing it, and ending it cleanly on pause).
- **Source pacing**: go-librespot's pipe backend is not realtime-paced, so `get_audio_stream`
  paces the read at the native rate. This back-pressures the daemon to ~realtime, keeping it
  only a fraction of a second ahead so transport commands land quickly.
- **Pause → clean EOF**: go-librespot keeps the pipe open while paused (it just stops writing),
  so `get_audio_stream` detects the gap and **ends the stream** — the player leaves the playing
  state (track preserved) and the next `playing` event re-streams. Resume works from both the
  Spotify app and the Music Assistant UI.
- **Format layering**: `audio_format` carries the source codec (Ogg Vorbis, for display) while
  `decoded_audio_format` is the `s16le` PCM actually on the wire. The provider emits the source
  format; Music Assistant resamples per player. `extra_input_args=["-fflags", "nobuffer"]`
  keeps the ffmpeg resample path low-latency.

## go-librespot binary

Resolved from `PATH` (`helpers.get_go_librespot_binary`). It is installed into the Docker image
and Home Assistant add-on from the upstream GitHub release at build time (see `Dockerfile.base`);
for manual installs put it on `PATH` (`brew install go-librespot` on macOS). The release binaries
statically link libvorbis/libogg/libFLAC, so the only shared-library dependency is `libasound2`,
already present in the base image. A clear error is raised when the binary is missing.

## Configuration

**Provider settings:** `mass_player_id` (the linked MA player; `__auto__` picks a playing player
then the first available) and `publish_name` (the name shown in the Spotify app).

**go-librespot `config.yml`** (written per instance into the cache dir as JSON — JSON is valid
YAML, avoiding a YAML dependency): pipe backend to `/dev/stdout` (`s16le`); `external_volume`
(MA owns volume, the daemon does not attenuate the PCM); `volume_steps: 100` (so its 0..max maps
1:1 to a percentage); a stable `device_id` derived from the instance id (so the Spotify app keeps
recognising the same device); zeroconf enabled (advertised on the streams bind interface);
`credentials.type: zeroconf` with persistence; and the API `server` on `127.0.0.1:<free-port>`.

## Event handling

A self-healing WebSocket listener (`_events_runner`) reconnects across daemon restarts and
dispatches `/events` messages:

| Event | Action |
|-------|--------|
| `active` | MA is the active Spotify device → mark session active |
| `inactive` | session ended → clear the active player and stop the MA player |
| `playing` | mark playing; on an external trigger, fire `play_media` on the target player (debounced) |
| `paused` / `stopped` | mark not-playing; the active stream then ends on its own |
| `metadata` | update `StreamMetadata` (title / artist / album / artwork / duration) |
| `seek` | update elapsed time |
| `volume` | apply the Spotify-side volume to the linked MA player |

## Playback & volume control

Transport commands reach the provider via `PluginProvider.on_source_control` and are translated
to go-librespot REST calls. Volume flows both ways: MA → Spotify via `POST /player/volume`;
Spotify → MA via the `volume` event (de-duplicated to avoid ping-pong, and ignored briefly right
after a session becomes active so the player's own volume wins).

**Initial volume sync.** With `external_volume` set, go-librespot never applies its
`initial_volume` config value and starts its Connect device state at 100%. The provider
therefore pushes the target player's live volume over REST when a session becomes active
(device selected in the Spotify app) and again when the source is claimed on a player,
so the Spotify app's volume slider adopts the player's actual volume — otherwise the first
volume tap in the app would send an absolute value computed from 100% and snap the player's
volume up. The push is unconditional: the last-sent cache tracks the last value exchanged,
not the daemon's current volume, which resets to its default on a new session or restart.

**Taking playback back.** When the user moves the active device away in the Spotify app, pressing
play in Music Assistant calls `POST /player/play` with the last seen context (and `skip_to_uri`
for the last track) — go-librespot activates this device unconditionally for a play request, so
this makes MA the active device again and resumes. When no prior context is known, a localized
"not the active Spotify device" error is raised instead.

## Multi-instance support

Each instance runs its own go-librespot daemon with its own cache/credentials dir, its own
loopback API port, and its own zeroconf advertisement, linked to one MA player — so several
Connect devices can coexist in one Music Assistant install.

## Known limitations

1. **Cold start (`can_initiate=False`)** — browse-and-play from a cold start is not offered yet;
   entry comes from the Spotify app, or by taking playback back during an existing session.
   go-librespot's native play makes a future "resume on select" feasible.
2. **Pause** surfaces as the player going idle (track preserved), not a held "paused" state — a
   true paused-and-held state for live sources would need streams/queue work.
3. **Sync-group elapsed time** can drift: the position comes from go-librespot's decode point,
   which leads a sync group's buffered output. Single players are unaffected.

## Related documentation

- **PluginProvider contract:** `music_assistant/models/plugin.py`
- **AudioSource MediaItem:** `music_assistant_models.media_items.AudioSource`
- **go-librespot API:** https://github.com/devgianlu/go-librespot/blob/master/API.md

---

*Update this document when the provider's design changes materially.*
