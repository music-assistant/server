# Spotify Connect Provider

The Spotify Connect provider makes any Music Assistant player (or sync group) appear as a
device in the official Spotify app via Spotify's Connect protocol. Selecting that device in
the Spotify app streams audio into Music Assistant; the player's transport and volume
controls are proxied back to Spotify. Music Assistant needs **no** Spotify Web API
credentials and **no** configured Spotify *music* provider for this.

The provider supports two interchangeable playback engines ("backends"), each in its own
subdirectory with its own README covering the internals:

- **[`soloist/`](soloist/README.md)** — wraps **Spotify Soloist**, Spotify's official
  headless Linux client (recommended).
- **[`go_librespot/`](go_librespot/README.md)** — wraps
  **[go-librespot](https://github.com/devgianlu/go-librespot)**, a community-built,
  reverse-engineered client.

## Module layout

```
spotify_connect/
├── provider.py      SpotifyConnectProvider: everything MA-facing (backend-agnostic)
├── base.py          SpotifyConnectBackend: the abstract backend contract
├── models.py        Normalized models shared across the boundary (BackendEvent, ...)
├── setup_flow.py    Multi-step setup: engine choice, Soloist terms/API key, player/name
├── helpers.py       Small shared utilities (device id, interface lookup)
├── soloist/         Official Spotify Soloist engine (backend + runtime + README)
└── go_librespot/    Community go-librespot engine (backend + client + README)
```

## The provider / backend split

**`SpotifyConnectProvider`** (`provider.py`) owns everything Music Assistant sees: the
AudioSource item and StreamDetails, target-player selection, the queue claim, the
play_media debounce, take-back-playback, volume-sync policy and live StreamMetadata. It
never talks Spotify: it drives a **`SpotifyConnectBackend`** (`base.py`) and consumes the
normalized `BackendEvent`s from `models.py`, so it does not know (or care) which engine is
running.

A backend owns everything specific to one way of talking to Spotify: daemon lifecycle and
supervision, credentials, the wire protocol, and audio delivery. It reports state changes
as `BackendEvent`s through a single async callback and answers `get_stream_source()` /
`get_audio_reader()` for the audio path.

### Shared provider behaviors (backend-independent)

- **AudioSource item**: one live item under the global "Live Inputs" node, played through
  the standard `play_media` flow (`exclusive`, `allow_external_trigger`, `can_initiate`).
  Starting it from MA resumes the last known Spotify context on this device.
- **Externally triggered playback**: a `PLAYING` event fires a debounced `play_media` on
  the target player (configured, or auto: a currently playing player, else the first
  available one).
- **Taking playback back**: when the user moved the active device away in the Spotify app
  and presses play in MA, the last seen context is (re)started on this device — the
  backend's `play()` contract includes claiming active device status.
- **Stream teardown → pause**: stopping the MA player or clearing the queue pauses the
  Spotify session, so the Spotify app reflects it.
- **Session inactive → bounded stop**: when the user deselects the device in the Spotify
  app, the active MA player is stopped (bounded, so a slow player cannot wedge it).
- **Live metadata**: title/artist/album/artwork/position are pushed into the active queue
  item's `StreamDetails.stream_metadata` — the same channel ICY radio metadata uses.
- **Volume dedupe/grace**: Spotify-side volume events are de-duplicated and ignored briefly
  after a session becomes active, so the player's own volume wins on (re)connect.

## Engine comparison

|  | Spotify Soloist | go-librespot |
|---|---|---|
| Origin | Official Spotify client | Reverse-engineered community client |
| Setup | Personal API key (created once, needs Premium) | None |
| Accounts | Free (with ads) or Premium | Premium-family accounts created before Dec 2024 |
| Max quality | Lossless up to 24-bit/44.1 kHz (Premium; actual quality opaque) | Ogg Vorbis 320 kbps |
| Audio delivery | PulseAudio pipe-sink → FIFO (`NAMED_PIPE`) | Daemon stdout pipe (`CUSTOM` stream) |
| Pause behavior | Pipe delivers silence → provider stops the player | Stream ends cleanly (EOF) |
| Volume | Two modes: pin at 100% (default) or sync with compensation | `external_volume`: MA owns volume |
| Risk profile | Binary downloaded from Spotify's CDN, 90-day build expiry, ToS grey area | May break when Spotify changes the protocol |

The setup flow defaults new instances to go-librespot and offers Soloist (with its terms
and API-key steps) where the platform supports it; existing pre-backend-split configs
migrate to go-librespot. Audio behavior such as quality, loudness normalization and
crossfade is governed by Spotify itself (see the per-engine READMEs).

## Multi-instance support

Each instance runs its own daemon with its own credentials/cache directory and its own
zeroconf advertisement, linked to one MA player — several Connect devices can coexist in
one Music Assistant install. Soloist instances share one managed binary install.

## Related documentation

- **PluginProvider contract:** `music_assistant/models/plugin.py`
- **AudioSource MediaItem:** `music_assistant_models.media_items.AudioSource`

---

*Update this document when the provider's design changes materially.*
