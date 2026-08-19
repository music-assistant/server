# Spotify provider

Music provider for Spotify: catalog/library access through the Spotify Web API, and
audio playback through one of two **playback backends** (chosen explicitly in the setup
flow, stored per instance):

- **`backends/librespot.py`** — the bundled community librespot fork. One
  `librespot --single-track` process per item, yielding the original Ogg Vorbis stream
  (passthrough). Simple, but relies on reverse-engineered internals: accounts created
  since December 2024 cannot use it. Source capacity: **2** concurrent streams.
- **`backends/soloist.py`** — Spotify Soloist, Spotify's official headless client. One
  `soloist --single-track` process per item, playing into a private PulseAudio capture
  sink (`helpers/pulse_capture.py`) whose FIFO is read back at realtime pace as
  s32le/44.1kHz PCM. Driven over the daemon's local WebSocket API (cold seek,
  buffering, completion). Source capacity: **1** concurrent stream.

The provider itself (`provider.py`) stays backend-agnostic: it owns the Web API,
parsing, StreamDetails and the audiobook chapter logic, and hands canonical
`spotify:<type>:<id>` URIs to the backend behind the `SpotifyPlaybackBackend` contract
(`backends/base.py`).

## Soloist specifics

The heavy lifting (managed binary install with 90-day build expiry, WebSocket client,
wire models) is shared infrastructure owned by the Spotify Connect provider
(`providers/spotify_connect/soloist/runtime.py`) — do not duplicate it here.

- **Setup**: explicit backend choice → ToS warning/consent → personal API key (created
  with a Premium account) → `soloist --pair` against a flow-private data dir, which the
  provider adopts into `<storage>/spotify/<instance_id>/soloist-data` on the next load.
  Existing configs without a backend value keep using librespot.
- **Capacity 1**: a Spotify account supports a single active Soloist session (verified:
  a second session terminates the first). The current and next item can therefore not
  be fetched concurrently — next-track preload, crossfade and Smart Fades across track
  boundaries need additional Spotify provider instances with *different* accounts. The
  generic provider-capacity handling (`max_concurrent_streams`) enforces this and
  selects a free instance where possible.
- **Pacing**: the capture FIFO is reader-clocked and Spotify's delivery cannot sustain
  accelerated reads (measured), so the FIFO is read at 1.0x realtime with a small
  initial burst. On a `buffering` status the sink is suspended so stall silence never
  enters the delivered PCM.
- **Delimiting**: the FIFO never ends on its own (the sink keeps rendering silence);
  WebSocket state and process exit delimit the item. Exit code 0 is not proof of
  complete PCM — the last observed playback position is validated against the item's
  duration. Exit code 10 (expired build) triggers a forced binary refresh.
- **Normalization**: soloist normalizes loudness per the account's setting and has no
  public switch; `audio.normalize_v2=false` is written to its prefs store before each
  spawn (best effort) so MA's own volume normalization stays the single loudness
  authority.
