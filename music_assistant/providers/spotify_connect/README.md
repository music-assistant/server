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
├── setup_flow.py    Multi-step setup: engine choice, Soloist terms/API key
├── helpers.py       Small shared utilities (device id, go-librespot binary lookup)
├── soloist/         Official Spotify Soloist engine (backend + runtime + README)
└── go_librespot/    Community go-librespot engine (backend + client + README)
```

## The provider / backend split

**`SpotifyConnectProvider`** (`provider.py`) owns everything Music Assistant sees: one
Connect device (a backend daemon plus its AudioSource item) per connected player, the
StreamDetails, the queue claim, the play_media debounce, take-back-playback, volume-sync
policy and live StreamMetadata. It never talks Spotify: it drives a
**`SpotifyConnectBackend`** (`base.py`) per daemon and consumes the normalized
`BackendEvent`s from `models.py`, so it does not know (or care) which engine is running.

A backend owns everything specific to one way of talking to Spotify: daemon lifecycle and
supervision, credentials, the wire protocol, and audio delivery. It reports state changes
as `BackendEvent`s through a single async callback and answers `get_stream_source()` /
`get_audio_reader()` for the audio path.

### Shared provider behaviors (backend-independent)

- **AudioSource item**: one live item under the global "Live Inputs" node, played through
  the standard `play_media` flow (`exclusive`, `allow_external_trigger`, `can_initiate`).
  Starting it from MA resumes the last known Spotify context on this device.
- **Externally triggered playback**: a `PLAYING` event fires a debounced `play_media` on
  the target player (an explicitly selected player, else the daemon's own connected
  player).
- **Taking playback back**: when the user moved the active device away in the Spotify app
  and presses play in MA, the last seen context is (re)started on this device — the
  backend's `play()` contract includes claiming active device status.
- **Stream teardown → release**: stopping the MA player or clearing the queue releases
  the Spotify session (`deactivate`), so the Spotify app drops the device as its
  playback target instead of staying tethered to it.
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
| Queue control | Session-side queue verbs (add-to-queue, shuffle, repeat) + queue/options events | Transport only |
| Volume | Two modes: pin at 100% (default) or sync with compensation | `external_volume`: MA owns volume |
| Risk profile | Binary downloaded from Spotify's CDN, 90-day build expiry, ToS grey area | May break when Spotify changes the protocol |

The setup flow defaults new setups to Soloist and presents both engines as expanded
choices. Existing pre-backend-split configs migrate to go-librespot. Loudness normalization,
crossfade and streaming quality are provider settings, applied by the engines themselves
(see the per-engine READMEs for the mechanics). Streaming quality is a ceiling, not a
guarantee: Spotify still downshifts on a slow connection and falls back when a track or the
account has no file at that tier, and what it actually delivered is not observable — so the
reported source format is the tier that was asked for, the same ceiling the Spotify apps
show.

## One device per connected player

The provider runs as a single instance driven by the connected-players multi-select in its
options: every selected player gets its own daemon with its own credentials/cache
directory (keyed by `{instance_id}_{safe_player_id}`), its own zeroconf advertisement and
its own AudioSource (`item_id` = player id). The advertised device name follows the
connected player's name through a template setting, so a rename restarts that daemon with
the new name. Players register after plugins load: daemons start (and stop) from player
lifecycle events through a lock-serialized reconcile. All daemons share the Soloist
managed binary install and API key.

## Related documentation

- **PluginProvider contract:** `music_assistant/models/plugin.py`
- **AudioSource MediaItem:** `music_assistant_models.media_items.AudioSource`

---

*Update this document when the provider's design changes materially.*
