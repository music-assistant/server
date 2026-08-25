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
- **Two formats per stream.** The engine decodes internally and never reports what it
  fetched, so `audio_format` states the configured ceiling — FLAC 24-bit/44.1kHz for
  music on the lossless tier, otherwise Ogg Vorbis at that tier's bitrate — which is
  what the Spotify apps show too, and only music is ever lossless. What actually arrives
  is the capture sink's PCM, declared as `decoded_audio_format`; that is the format the
  streams core hands ffmpeg, while `audio_format` is for display. Note MA classifies
  24-bit/44.1kHz as hi-res rather than lossless.
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
  sample, so the cut position does not matter. Only consecutive tracks are stitched; a
  podcast episode or audiobook chapter is played on its own.
- **Use the engine's transport before respawning it.** A next-track lands on the item
  that was fed one ahead, so the engine is told to skip to it and the session
  survives. Tearing a session down and spawning another costs a login, an
  activate and a re-feed, which is seconds — and the daemon never exits on its own, so
  nothing is gained by waiting for it either: it is closed straight away.
- **A session in use is never cut short**: the engine allows one session, so an item
  the running one cannot serve would otherwise restart it and truncate whatever it is
  still delivering. That happens at boundaries the session does not drive — a podcast
  episode or audiobook chapter (never stitched), the same track twice in a row, or
  another player — so those are reported as `ProviderStreamLimitError` instead. A
  speculative prepare then gives up softly, and the real request, made once the other
  item has been released, gets the session. The cost is a cold start at those
  boundaries rather than a warm buffer.
- **The Spotify app can reach in, and that ends the session.** The engine always
  advertises itself as a Connect device and offers no way to suppress that, so the
  device is listed in the user's Spotify apps for as long as Music Assistant is
  playing — named `SOLOIST_DEVICE_NAME`, deliberately *not* the plain "Music
  Assistant" the Spotify Connect provider advertises by default, so the two do
  not arrive under one name. Three things the user can do there are handled:
  - **Move playback to another device**, which shows up as `is_active` going false
    on `device_changed`/`auth_state` (`playback_state.is_active` is optional and
    rides on deltas, so it is ignored). Only a loss of the active status the session
    itself claimed counts — a daemon is inactive until `_play` activates it.
  - **Start something else on this device**, which shows up as the engine moving
    somewhere it was never sent while the current item is still part-way through
    (`_ItemAudio.mid_play`). Only the item fed behind the current one is exempt —
    a skip in the app lands there, which is also where the queue goes next, so the
    two stay in step. The engine's own autoplay and the item it restores at startup
    both fail the `mid_play` test, which is what keeps them out of it. Note the
    last 10s of an item is a blind spot by design: judging a boundary needs that
    allowance, so a takeover inside it is missed rather than risking a false one on
    every track.
  - **Pause**, which is put back a couple of times (an accidental tap) and then
    taken at face value.

  All three end the session and hold off a replacement for `_APP_CONTROL_COOLDOWN_S`
  through `SoloistAppControlError`. That hold is the point: without it the next queue
  item spawns a daemon that claims the Connect device straight back off whatever the
  user just moved to. It is a `ProviderStreamLimitError` so the queue treats it as
  capacity — the item stays playable, other providers get a chance at it, and an
  explicit play stops the queue with a message saying what happened.
- **Readiness comes from the session**: the core's blind next-item pre-buffer is
  suppressed for a realtime source (`controllers/streams/audio.py`), because the next
  item's audio does not exist until the session gets there. The session calls
  `prepare_next_audio_buffer()` when it does, identifying the item **by URI** (a queue
  reorder may have moved it).
- **The engine never crossfades**: its own crossfade is written off in the prefs before
  every spawn, so each track's audio arrives clean from its first sample. Music Assistant
  mixes the boundary itself from the tail it holds back, which is what keeps waveforms,
  beat grids and light sync aligned with what is heard — a fade baked into the bytes
  would shift every track's start against its analysis. It also means smart fades work
  on Spotify audio.
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
  the item duration, but it is bounded, and completeness is validated against the
  furthest playback position the engine reported for it. The **last** item
  of a run gets no track change to cut it on, so a stop/idle/pause snapshot at its own
  end arms a bounded tail drain instead; a pause part-way through is treated as app
  interference, and the engine resuming cancels the drain. A channel is served **once**:
  its audio is handed over as it is consumed, so a repeated track (or repeat wrapping
  back to the top) starts a fresh session rather than replaying a drained channel.
  Exit code 10 (expired build) triggers a forced binary refresh.
- **Normalization**: exactly one of the two normalizes, and a provider option decides
  which. On (the default) the engine's own normalizer is enabled and the provider
  declares `delivers_normalized_audio(streamdetails)` for the queue that session serves,
  which makes the streams core skip normalization for those items — another queue gets
  the configured answer instead, since the running session says nothing about it.
  Spotify uses values computed over its whole catalogue and will not push a quiet track
  past its remaining headroom, and MA correcting that again would be normalizing twice,
  the second time against a measurement of Spotify's own output.
  Skipping also means the loudness analyzer declines the stream, so no measurement is
  stored: a value measured on one backend's output can never be applied to the other's,
  with no erase step needed. Off, `audio.normalize_v2=false` is written and MA measures
  and normalizes exactly as it does for any other source.
