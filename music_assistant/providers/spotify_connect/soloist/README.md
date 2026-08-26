# Soloist backend

Wraps **Spotify Soloist**, Spotify's official headless Linux client (released August
2026), as a Spotify Connect engine. Being the official client it supports every account
type (Free with ads, Premium, lossless up to 24-bit/44.1 kHz) and survives protocol
changes — at the cost of a managed binary download and a per-user API key.

```
   Spotify app  (mobile / desktop / web)
        │   Connect protocol: mDNS discovery + audio
        ▼
   soloist daemon  (one per instance, spawned with PULSE_SINK=<capture sink>)
        │   plays decoded PCM into a PulseAudio pipe-sink
        ▼
   PulseAudio module-pipe-sink ──▶ FIFO (s32le / 44.1 kHz / 2ch)
        │
        ▼
   MA streams controller  (NAMED_PIPE: ffmpeg reads the FIFO, -readrate paced)
        │
        ▼
   Music Assistant player
```

## Module layout

- **`backend.py`** — `SoloistBackend`: daemon lifecycle/supervision, the capture sink,
  volume modes, event translation.
- **`runtime.py`** — everything needed to run the daemon: `SoloistBinaryManager` (managed
  binary install), `SoloistClient` (local WebSocket API) and the typed wire models. Also
  the import surface for other providers — the Spotify music provider's own soloist
  playback backend reuses it rather than shipping a second copy.
- **`prefs.py`** — `write_audio_prefs`: the classic desktop-client prefs stores the engine
  reads at startup (crossfade, normalization, quality tier).

## Binary management (`SoloistBinaryManager`)

Spotify does not distribute Soloist through package managers; the manager downloads the
tarball for the host architecture from Spotify's CDN — **only after explicit user consent**
recorded in the setup flow — validates it (tar structure, ELF magic and architecture),
and installs it atomically (write-aside + rename, with rollback). All instances share one
install under the MA storage dir, serialized by a module-level lock.

Soloist builds **expire 90 days after their build date** (the daemon then exits with code
10). Three layers keep that invisible to users:

1. **Install-time**: `ensure_fresh()` replaces a build nearing/at expiry (metadata
   timestamp parsed from `--version`), keeping a still-valid binary on download failure.
2. **Daily refresh loop**: replaces the build ahead of expiry and respawns the daemon.
   The comparison baseline is the digest of the build *this* instance's daemon runs, so an
   update installed by a sibling instance is picked up too.
3. **Exit-code-10 recovery**: a forced re-verification (bypassing the short verify cache)
   before the supervisor respawns.

A recently-verified cache (60 s) keeps concurrent instance startups from re-checking; the
API key is only ever passed on the daemon's argv and redacted from its captured
output (stderr is merged into stdout, which the log reader redacts).

## Audio capture (PulseAudio pipe-sink)

Soloist has no pipe/stdout output of its own — it plays into an audio server. The backend
uses the shared `helpers/pulse_capture.py` infrastructure: a private per-MA PulseAudio
daemon hosting one `module-pipe-sink` per instance, delivering s32le/44.1kHz/2ch PCM into
a FIFO. The daemon is spawned with `PULSE_SERVER`/`PULSE_SINK` pointing at its sink.

- `get_stream_source()` is a **pure read** (it also runs from queue preload): it returns
  the FIFO as a `NAMED_PIPE` source or raises when no usable sink exists. ffmpeg reads the
  FIFO directly and is the single pacing owner (`-readrate` with a small initial burst).
- A **generation watcher** detects a restarted pulse daemon and recreates the sink;
  recreation always respawns the soloist daemon (it holds the sink name in its spawn env).
- Failed sink volume operations **fail closed**: sink and daemon are torn down and rebuilt
  rather than risking audio through a sink with an unknown gain.
- The pipe-sink **never signals end of stream**, so `stream_ends_on_pause` is False: the
  provider actively stops the MA player on a `PAUSED` event (bounded, replaced by a quick
  resume). The sink renders only while a client is connected — it emits silence while the
  daemon holds its stream open and nothing at all once the daemon drops it, and suspending
  the sink does not produce an EOF either. A reader that outlives the pause blocks until
  the stall timeout, so MA must end the stream itself.

## Volume modes

- **`player_only` (default, quality-first)**: the daemon is pinned at 100% so unity-gain
  PCM reaches the FIFO; the MA player owns the audible volume. Off-100 reports from the
  daemon (user dragging the app slider) re-pin it; a failed pin marks the cached volume
  unknown so the next snapshot retries.
- **`sync_spotify`**: the Spotify app's slider and the player volume stay in sync. The
  daemon attenuates its PCM with Spotify's **cubic** volume curve; the sink compensates
  with the reciprocal percentage (`sink_pct = 10000 / spotify_pct`), which is the *exact*
  linear inverse because PulseAudio's software volume is cubic in the percentage as well
  (`pa_sw_volume_to_linear(p) = (p/100)³`) — validated by capture measurement. A large
  upward volume jump can cause a brief full-scale artifact until the compensation lands
  (the notification arrives after the daemon already raised its level); this is inherent
  and documented in the config option.

## Local control (WebSocket API)

The daemon is spawned with `--ws 127.0.0.1:0` and writes the bound address to
`ws.addr`/`ws.port` files in its data dir, which `SoloistClient` polls. One connection
streams JSON events (auth/session/playback/track/volume, with metadata nested in entity
`decorations`); commands (`play`, `pause`, `seek`, `set_volume`, `activate`, ...) are
acknowledged FIFO per command type. `play()` claims active device status first
(`activate`) — a bare play would start local playback without a Connect transfer, leaving
the Spotify apps unaware.

An `auth_state` reporting `logged_in: false` is always a `SESSION_INACTIVE`, never an
error: the daemon keeps advertising itself for Connect while logged out, so awaiting a
first pairing, the user signing out and another account taking the device over all leave
it usable. MA never signs the daemon in — any Spotify account may claim it from its app.

## Audio behavior settings

The public CLI/WebSocket surface exposes no audio-behavior controls, and the Spotify apps
grey these settings out for Connect targets. The engine does read the classic
desktop-client prefs stores in its data dir at startup, which is how the provider's
loudness normalization and crossfade settings are applied: the backend rewrites
`audio.crossfade_v2`, `audio.crossfade.time_v2` (milliseconds — sub-second values
silently disable crossfade) and `audio.normalize_v2` before every daemon spawn, in both
the global `settings/prefs` and every per-user `settings/Users/*/prefs` (per-user values
override global ones per key; the engine scrubs foreign keys from the global store when
it rewrites it, hence the refresh on every spawn). Crossfade only reaches boundaries fed
through the engine's queue: a real album or playlist context is never crossfaded
(measured), and that path is not gapless either — the outgoing file's trailing silence
is left in place.

The streaming quality setting travels the same way, as `audio.play_bitrate_enumeration`,
`audio.play_bitrate_non_metered_enumeration` and the `audio.play_bitrate_non_metered_migrated`
marker that makes the engine honor the non-metered value. Measured against build 1.3.7.349
(bytes fetched for a whole 4:20 track): `2` ≈ 96 kbps, `3` ≈ 160 kbps, `4` ≈ 320 kbps and
`5` lossless FLAC (~810 kbps). **`5` is the ceiling** — values outside `1`-`5` are rejected
and silently fall back to ~160 kbps, so an unrecognized tier must never reach the file.
That measurement only covers the wire: the capture sink always delivers s32le whatever the
engine fetched, so the source bit depth cannot be read off the audio at all and the
`FLAC_FLAC_24BIT` name in the binary is the only indication either way.

The delivered quality is **not** observable. The engine keeps no quality field on the
WebSocket API, its audio cache is encrypted, and the log templates that would name the
codec (`AudioRendererImpl ... format [...]`, `FileStreamer file average bitrate`) sit
behind a log level with no exposed control: the daemon's real optstring is
`hn:D:C:z:d:i:AVw:k:s:p` (no `-v`; the `-v, --verbose` string in the binary is dead text,
and no environment variable raises the level either).

So the configured tier is what gets reported as the source format — the same ceiling the
Spotify apps show, and the same claim the Spotify music provider makes. Spotify serves
lossless for music only, so an episode or audiobook chapter is reported as Ogg Vorbis
whatever the tier says; a Connect session plays whatever the app picked, so the item
playing when the stream starts is the only media-type signal there is. `_capture_format`
describes the PCM the FIFO delivers and is reported separately as the decoded format, so
every decision about the bytes follows it and the tier claim stays display-only.

## Security

- The data dir persists the Spotify device identity and login session → `chmod 0700`.
- The API key is stored encrypted (secure string), passed only on argv, redacted in logs.
- The capture FIFO directory is owner-only; stale FIFOs are swept on daemon start.
