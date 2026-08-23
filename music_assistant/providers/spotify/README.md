# Spotify provider

Music provider for Spotify: catalog/library access through the Spotify Web API, and
audio playback through one of two **playback backends** (chosen explicitly in the setup
flow, stored per instance):

- **`backends/librespot.py`** — the bundled community librespot fork. One
  `librespot --single-track` process per item, yielding the original Ogg Vorbis stream
  (passthrough). Simple, but relies on reverse-engineered internals: accounts created
  since December 2024 cannot use it.
- **`backends/soloist.py`** — Spotify Soloist, Spotify's official headless client. One
  continuous session, fed one track ahead, playing into a private PulseAudio capture
  sink (`helpers/pulse_capture.py`) whose FIFO is read back slightly above realtime pace
  as s32le/44.1kHz PCM. Driven over the daemon's local WebSocket API.

Source capacity is **2** on either backend — for librespot two parallel fetches, for
Soloist the item that is ending and the item that continues from the same session.

The provider itself (`provider.py`) stays backend-agnostic: it owns the Web API,
parsing, StreamDetails and the audiobook chapter logic, and hands canonical
`spotify:<type>:<id>` URIs to the backend behind the `SpotifyPlaybackBackend` contract
(`backends/base.py`).

## Soloist specifics

The heavy lifting (managed binary install with 90-day build expiry, WebSocket client,
audio prefs, wire models) is shared infrastructure owned by the Spotify Connect provider
(`providers/spotify_connect/soloist/`) — do not duplicate it here.

- **Setup**: explicit backend choice → ToS warning/consent → personal API key (created
  with a Premium account) → `soloist --pair` against a flow-private data dir, which the
  provider adopts into `<storage>/spotify/<instance_id>/soloist-data` on the next load.
  Existing configs without a backend value keep using librespot, and a fresh setup
  preselects it too.
- **The pairing has to be the same account as the sign-in**, or the library and the audio
  come from different places. The engine records the paired account as its per-user state
  directory (`settings/Users/<username>-user`, the canonical username, i.e. the signed-in
  id lowercased), which is the only place that identity is written down. Checked after
  pairing and before keeping an existing pairing on reconfigure; a session whose account
  cannot be read never blocks setup, mirroring the librespot credential check.
- **Streaming quality** is a provider option, shared with the Spotify Connect provider's
  tiers and defaulting to lossless. It is a ceiling: Spotify serves the best the account
  is entitled to below it. Hidden on librespot, which passes Spotify's own file through.
- **One daemon per data directory**: the engine refuses to start while another
  session still holds it, so `_session_lock` covers the teardown as well as the
  bookkeeping — a replacement session is never spawned before the previous daemon
  has been reaped.
- **One session, fed one track ahead**: a Spotify account supports a single active
  Soloist session, so items are not fetched one by one. The session plays consecutive
  tracks continuously — `play(uri)` for the first, `add_to_queue(uri)` for the follower
  — and that one continuous audio stream is split into ordinary per-item streams: an
  item's stream ends where the session reports moving on, and the next item's stream
  begins there. Played back to back the items reproduce the session's audio sample for
  sample, so the cut position does not matter and Spotify's crossfade simply lives
  inside the bytes. Only consecutive tracks are stitched; a podcast episode or audiobook
  chapter is played on its own.
- **Readiness comes from the session**: the core's blind next-item pre-buffer is
  suppressed for a realtime source (`controllers/streams/audio.py`), because the next
  item's audio does not exist until the session gets there. The session calls
  `prepare_next_audio_buffer()` when it does, identifying the item **by URI** (a queue
  reorder may have moved it).
- **Crossfade comes from the queue**: Music Assistant cannot crossfade audio it is not
  mixing, so the queue's own crossfade preference is written into the engine's prefs
  before every spawn. Its unit is milliseconds and sub-second values silently disable
  crossfade, which the seconds-based queue setting can never produce. Changing the
  setting mid-playback takes effect on the next playback.
- **Pacing**: the capture FIFO is reader-clocked — how fast it is read *is* how fast
  the engine plays, because the pipe sink applies no rate limit of its own (read
  unpaced, PulseAudio renders silence rather than pushing back, and the session runs
  off the end of its content). It is read at **1.1x** with a small (1s) initial burst,
  both ear-tested: the surplus banks the cushion (~6s per minute) that carries an item
  boundary, while a large burst window is unpaced and audibly destabilizes track
  starts. Do not add ffmpeg-side pacing on top of this. The pacing clock restarts after
  any gap instead of making it up, since catching up would mean exactly that unpaced
  burst.
- **Backpressure is ours to apply**: reading above realtime means the engine runs ahead
  of the player, and nothing upstream stops it. `_MAX_RETAINED_S` caps the
  captured-but-undelivered audio and suspends the capture sink past it, which stalls
  the engine until the player catches up. Without that cap the cushion grows without
  limit and the engine's own item eventually runs more than one queue item ahead —
  which breaks the URI match the readiness signal depends on. The same gate keeps
  rebuffering and pause silence out of the delivered PCM, so all sink control goes
  through one place (`_apply_sink_state`).
- **Delimiting**: the FIFO never ends on its own (the sink keeps rendering silence), so
  WebSocket state delimits the items. An item stream is deliberately **not** capped at
  the item duration — with crossfade it carries the head of the next track — but it is
  bounded, and completeness is validated against the furthest playback position the
  engine reported for it, with the crossfade added to the tolerance. The **last** item
  of a run gets no track change to cut it on, so a stop/idle/pause snapshot at its own
  end arms a bounded tail drain instead; a pause part-way through is treated as app
  interference, and the engine resuming cancels the drain. A channel is served **once**:
  its audio is handed over as it is consumed, so a repeated track (or repeat wrapping
  back to the top) starts a fresh session rather than replaying a drained channel.
  Exit code 10 (expired build) triggers a forced binary refresh.
- **Normalization**: exactly one of the two normalizes, and a provider option decides
  which. On (the default) the engine's own normalizer is enabled and the provider
  declares `delivers_normalized_audio`, which makes the streams core skip normalization
  for these items — Spotify uses values computed over its whole catalogue and will not
  push a quiet track past its remaining headroom, and MA correcting that again would be
  normalizing twice, the second time against a measurement of Spotify's own output.
  Skipping also means the loudness analyzer declines the stream, so no measurement is
  stored: a value measured on one backend's output can never be applied to the other's,
  with no erase step needed. Off, `audio.normalize_v2=false` is written and MA measures
  and normalizes exactly as it does for any other source.
