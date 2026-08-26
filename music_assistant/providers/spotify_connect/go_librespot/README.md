# go-librespot backend

Wraps **[go-librespot](https://github.com/devgianlu/go-librespot)**, a community-built,
reverse-engineered Spotify Connect client, as the community Spotify Connect engine. It needs
no API key or managed download, but only works with Premium-family accounts created before
December 2024 and may break whenever Spotify changes the protocol.

```
   Spotify app  (mobile / desktop / web)
        │   Connect protocol: mDNS discovery + audio
        ▼
   go-librespot daemon  (one per connected player)
        │   decodes Ogg Vorbis → s16le PCM on stdout, driven by
        │   GoLibrespotClient over its local HTTP + WS API
        ▼
   get_audio_stream  (CUSTOM stream: backpressured, ends on pause)
        │
        ▼
   MA streams controller  (ffmpeg resample, per player)
        │
        ▼
   Music Assistant player
```

## Module layout

- **`backend.py`** — `GoLibrespotBackend`: daemon lifecycle/supervision, config
  generation, the stdout audio reader, event translation.
- **`client.py`** — `GoLibrespotClient`: REST control (`POST /player/{resume,pause,next,
  prev,seek,volume,play}`) plus the `/events` WebSocket. `204 No Content` means "no
  active session" and is treated as a no-op.

## Daemon & configuration

The binary is resolved from `PATH` (installed into the Docker image / HA add-on at build
time; `brew install go-librespot` for manual macOS installs). Each daemon writes a
`config.yml` into its own cache dir (keyed by its identity key) — emitted as JSON (valid YAML) to avoid a YAML dependency
and quoting pitfalls:

- `audio_backend: pipe` with `audio_output_pipe: /dev/stdout` (`s16le`): the daemon writes
  decoded PCM to its stdout, which the backend captures. The subprocess pipe always has a
  reader, so the daemon's non-blocking pipe open never fails.
- `external_volume: true`: the daemon never attenuates the PCM — MA/the player owns the
  audible volume. Volume events still flow both ways so the app slider stays in sync.
- A stable `device_id` derived from the daemon's identity key (the Spotify app keeps
  recognising the same device), zeroconf advertised on the streams bind interface,
  credentials persisted per daemon.
- The local HTTP/WS API binds to `127.0.0.1` on a free port per daemon.

The daemon is supervised: restarts on exit, a fatal event (→ `unload_with_error`) after
repeated failures. A self-healing WebSocket listener reconnects across daemon restarts and
translates `/events` messages into normalized `BackendEvent`s.

## Audio transport

`StreamType.CUSTOM`: `get_audio_stream` reads the daemon's stdout and yields PCM; the
streams controller's realtime pacer (ffmpeg `-readrate` with a small initial burst) is the
single pacing authority, while stdout backpressure keeps the daemon only a fraction of a
second ahead so transport commands land quickly.

**Pause → clean EOF**: the daemon keeps the pipe open while paused (it just stops
writing), so the reader detects the gap and ends the stream — the player leaves the
playing state and the next `playing` event re-streams (`stream_ends_on_pause` is True).

`audio_format` carries the source codec (Ogg Vorbis, for display) while
`decoded_audio_format` is the s16le PCM actually delivered; MA resamples per player.

## Volume

With `external_volume` set the daemon starts its Connect device state at 100% and ignores
`initial_volume`; the provider pushes the target player's live volume when a session
becomes active and when the source is claimed, so the Spotify app's slider adopts the
player's actual volume instead of snapping it to a value computed from 100%.

## Taking playback back

`play()` (`POST /player/play` with the last seen context and `skip_to_uri`) activates
this device unconditionally, so pressing play in MA after the user moved playback away
makes MA the active device again and the Spotify apps follow.

## Audio behavior settings

The provider's loudness normalization and crossfade settings are written into the
generated config: `normalisation_disabled` (normalization targets -14 LUFS when on) and
`crossfade_duration` (milliseconds, 0 = off). The crossfade key needs go-librespot
>= 0.8.0 — older daemons ignore unknown config keys, so it is written unconditionally.

The streaming quality setting maps onto `bitrate`, which only accepts 96, 160 and 320.
go-librespot has no lossless support, so the lossless tier is capped at 320 rather than
dropped — the setting is a ceiling, and this engine's ceiling is lower.

## Known limitations

1. **Cold start**: starting from MA resumes the last known Spotify context; without any
   prior context (fresh install) a localized error points the user to the Spotify app.
2. **Sync-group elapsed time** can drift: the position comes from the daemon's decode
   point, which leads a sync group's buffered output. Single players are unaffected.

## Related documentation

- **go-librespot API:** https://github.com/devgianlu/go-librespot/blob/master/API.md
