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
  the import surface for other providers (e.g. a future Spotify music provider).

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
API key is only ever passed on the daemon's argv and redacted from logged stderr.

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
- The pipe-sink emits **silence when paused** (no EOF), so `stream_ends_on_pause` is
  False: the provider actively stops the MA player on a `PAUSED` event (bounded, replaced
  by a quick resume).

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

An `auth_state` reporting `logged_in: false` after a previously seen login raises
`AUTH_REQUIRED` (→ provider unloads with a login error routing the user through setup);
a fresh daemon reporting `logged_in: false` while awaiting pairing is normal.

## Audio behavior settings

Quality, loudness normalization, crossfade and automix are governed by Spotify — the
public CLI/WebSocket surface exposes no controls for them, and the Spotify apps grey these
settings out for Connect targets. The engine does read the classic desktop-client prefs
file in its data dir (verified: `audio.crossfade_v2`, `audio.normalize_v2`), which is the
basis for planned opt-in config entries.

## Security

- The data dir persists the Spotify device identity and login session → `chmod 0700`.
- The API key is stored encrypted (secure string), passed only on argv, redacted in logs.
- The capture FIFO directory is owner-only; stale FIFOs are swept on daemon start.
