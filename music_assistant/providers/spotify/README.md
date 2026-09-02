# Spotify provider

Music provider for Spotify: catalog/library access through the Spotify Web API, and
audio playback through one of two **playback backends** (chosen explicitly in the setup
flow, stored per instance):

- **`backends/librespot.py`** — the bundled community librespot fork. One
  `librespot --single-track` process per item, yielding the original Ogg Vorbis stream
  (passthrough). Simple, but relies on reverse-engineered internals: accounts created
  since December 2024 cannot use it.
- **`backends/soloist.py`** — Spotify Soloist, Spotify's official headless client. One
  `soloist --single-track` run per item, playing into a private PulseAudio capture
  sink (`music_assistant/helpers/pulse_capture.py`) whose FIFO is read back slightly above realtime pace
  as s32le/44.1kHz PCM. Driven over the daemon's local WebSocket API.

Source capacity is **2** on librespot (two parallel fetches) and **1** on Soloist: the
account's single stream. The queue's prefetch takes the freed slot the moment an item
has fully arrived.

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
- **A lost pairing is caught from the engine's own report.** The daemon narrates its
  startup on stdout — `restoring session`, then `logged in as <username>` — and with
  nothing to restore it advertises itself for pairing instead (`waiting for login -
  connect to "<name>" from your Spotify app`) and sits there. That line fails the item and
  raises `LoginFailed(soloist_pairing_required)`, which takes the provider out of service
  and sends the user back through setup — but only once it is confirmed against the stored
  session: a daemon still restoring one can announce itself the same way, and acting on
  the line alone would fail playback on a perfectly good pairing.
- **A pairing Spotify no longer accepts** — a password change, "sign out everywhere", a
  revoked device — **is a blind spot.** What the engine does then has never been captured,
  and the first thing to establish is whether it wipes the stored session — which the case
  above catches only if the daemon announces itself for pairing afterwards, since that
  line is what triggers the check — or keeps it, which nothing detects. `auth_state`
  cannot carry that decision on its own: `logged_in=false` is also what a healthy daemon
  reports before it finishes restoring, and nothing tells it apart from a daemon that
  simply cannot reach Spotify. Nor can a guessed stdout marker, which would fail healthy
  playback. So playback fails with a plain `Timeout waiting for audio data`: the queue's
  readiness budget (`BUFFER_READY_TIMEOUT`, plus any capacity wait) starts before the
  session's own `_STARTUP_TIMEOUT_S` and runs out first, cancelling the producer where it
  waits. That also leaves `_raise_startup_error`'s "never logged in" branch to the case it
  can still reach — a login lost after it was established. The daemon's own output is
  logged at DEBUG behind the `[soloist]` prefix; capturing a real revocation is what
  unblocks handling it.
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
  run still holds it, so `_run_lock` covers the teardown as well as the
  bookkeeping — a replacement run is never spawned before the previous daemon
  has been reaped.
- **One engine run per item (single-track mode)**: every Spotify URI is played by its
  own daemon, spawned with `--single-track`, which restores the stored session without
  advertising a Spotify Connect device, plays that one item with shuffle and repeat off,
  and exits when it finishes. The provider is an ordinary single-stream source: the
  item's `AudioBuffer` consumes the run's PCM like any other provider's stream, and no
  track is ever stitched to another provider-side.
- **One stream slot**: a Spotify account supports a single active stream, and the run
  is it. A request for another item while a run is live is reported as
  `ProviderStreamLimitError` capacity: a speculative prepare gives up softly (or waits
  its slot budget out), and the real request gets the slot once the stream holding it
  is released. The slot frees the moment an item's audio has *fully arrived* — the
  buffer's fill closes the provider stream at EOF and the fill-complete hook starts
  fetching the next queue item right away, so the engine's ~1.1x delivery surplus
  compounds across a listening session. A new request for the very item being
  delivered is a seek of it: the run restarts at the target. A continuation the seek
  replaced (an audiobook's next chapter) ends with `StreamSupersededError` instead of
  taking the run back.
- **The Spotify app cannot reach in.** Single-track mode advertises no Connect device
  and disables remote control and transfer for its playback context, so there is
  nothing for the app to move, pause or take over. Playing on the account from another
  device can still kill the stream Spotify-side; that ends the run as an incomplete
  delivery and the queue moves on.
- **The engine never crossfades**: its own crossfade is written off in the prefs before
  every spawn, so each track's audio arrives clean from its first sample. Music Assistant
  mixes the boundary itself from the tail it holds back, which is what keeps waveforms,
  beat grids and light sync aligned with what is heard — a fade baked into the bytes
  would shift every track's start against its analysis. It also means smart fades work
  on Spotify audio.
- **Pacing**: the capture FIFO is reader-clocked — how fast it is read *is* how fast
  the engine plays, because the pipe sink applies no rate limit of its own (read
  unpaced, PulseAudio renders silence rather than pushing back, and the run runs
  off the end of its content). It is read at **1.1x** with a small (1s) initial burst,
  both ear-tested: the surplus is the lead a boundary's crossfade is built from, while
  a large burst window is unpaced and audibly destabilizes track starts. Do not add
  ffmpeg-side pacing on top of this. The pacing clock restarts after any gap instead
  of making it up, since catching up would mean exactly that unpaced burst.
- **Backpressure is the cushion's**: the FIFO holds well under a second, so the reader
  drains it continuously into a small bounded cushion the item stream consumes. A full
  cushion — the item's buffer is at its memory-tiered capacity — suspends the capture
  sink, which pauses the engine; space resumes it. The engine's pause silence is kept
  out of the delivered PCM the same way, and leading infrastructure silence and the
  sink's tail padding are trimmed per run.
- **Delimiting**: the run ends when the daemon exits — the item finished, stopped or
  was refused. Whatever the sink renders after the exit is padding and is dropped. The
  delivered length is validated against the item's duration, so an engine that refuses
  an item (unavailable to the account or region) surfaces as an incomplete delivery
  instead of a silently 'completed' track. A cold seek is confirmed against the
  engine's position reports before any PCM is released, with the sink held down until
  then. Exit code 10 (expired build) triggers a forced binary refresh.
- **Normalization**: exactly one of the two normalizes, and a provider option decides
  which. On (the default) the engine's own normalizer is enabled and the provider
  declares `delivers_normalized_audio(streamdetails)` for the item its run is serving,
  which makes the streams core skip normalization for it — any other item gets
  the configured answer instead, since a run says nothing about anything else.
  Spotify uses values computed over its whole catalogue and will not push a quiet track
  past its remaining headroom, and MA correcting that again would be normalizing twice,
  the second time against a measurement of Spotify's own output.
  Skipping also means the loudness analyzer declines the stream, so no measurement is
  stored: a value measured on one backend's output can never be applied to the other's,
  with no erase step needed. Off, `audio.normalize_v2=false` is written and MA measures
  and normalizes exactly as it does for any other source.
