# Feature: Local Buffer

## Problem

Currently, Music Assistant will only buffer about 30s of audio due to unclear
legal constraints. 30s is considered safe / legal because streaming providers
such as Apple Music, Deezer and others also provide snippets of 30s length to
the public internet. But not every media item in Music Assistant has legal
constraints by streaming providers. Local media library, podcasts, etc. all
don't have this legal restriction but suffer from the same technical constraints.
If the connection drops for more than 30s, the stream is stopped and only resumed
if the network comes back.

## Analysis — Four Independent Bottlenecks

Tracing the full playback pipeline reveals **four separate rate-limiting
mechanisms**, one per player type, and **none of them are provider-aware**
today. The 30-second effect the user experiences comes from different hardcoded
constants depending on which player they use.

### Player delivery types

| Player type | How audio reaches the player | Examples |
|---|---|---|
| **SendSpin** | MA pushes PCM F32LE → PushStream → encodes to per-client codec (FLAC/Opus) → WebSocket/WebRTC | Mobile app, web app, Chromecast bridge, AirPlay bridge, Local Audio bridge |
| **HTTP flow** | MA gets PCM → ffmpeg encodes to FLAC/MP3 → HTTP response → player fetches URL | Sonos, DLNA |
| **Snapcast** | MA gets PCM → ffmpeg → Snapcast TCP source → Snapcast server → Snapclient | Snapcast speakers |
| **AirPlay** | MA gets PCM → cliraop/cliap2 binary → AirPlay device | AirPlay speakers |

### Bottleneck 1: SendSpin — `_PRODUCER_BUFFER_LIMIT_US = 30s`

**File:** `music_assistant/providers/sendspin/playback.py:72`

```python
_PRODUCER_BUFFER_LIMIT_US = 30_000_000  # 30 seconds
```

**Pipeline:**
```
MA get_stream() PCM F32LE
  → _produce_pending_chunks() slices PCM into 100ms chunks
    → asyncio.Queue (max 64 chunks = 6.4s)
      → _commit_pending_chunks()
        → push_stream.prepare_audio(pcm, format)  ← PushStream encodes per-client
        → push_stream.commit_audio()               ← sends via WebSocket/WebRTC
        → push_stream.sleep_to_limit_buffer(30s)   ← BACKPRESSURE: sleeps if
                                                      server is >30s ahead of clock
```

**Used at:**
- Line 865: `push_stream.sleep_to_limit_buffer(_PRODUCER_BUFFER_LIMIT_US)` — if the
  committed timeline is more than 30s ahead of real-time, the producer sleeps.
- Line 551: Join-catchup queue sizing.
- Line 1419: Buffer drain safety timeout.

**The code already distinguishes live vs buffered** at line 740-741:
```python
is_live = media.media_type in _LIVE_MEDIA_TYPES  # {RADIO, AUDIO_SOURCE, ...}
push_stream.set_live_source(is_live)
```
The comment above (line 52-54):
> "Buffered types (tracks, podcasts, etc.) race ahead and fill the queue
> naturally, so the min_buffer startup wait is pure latency."

So the concept of "let buffered content race ahead" **already exists** in the
code. But it only distinguishes live vs non-live media types, not provider
constraints. A Spotify track and a local FLAC get the same 30s limit.

**Impact:** The mobile app can only ever be 30s ahead of playback. A network
dropout longer than 30s exhausts the buffer and playback stops.

### Bottleneck 2: HTTP flow — ffmpeg `-readrate 1.1`

**File:** `music_assistant/controllers/streams/controller.py:1025`

```python
extra_input_args=["-readrate", "1.1", "-readrate_initial_burst", "5"],
```

**Pipeline:**
```
get_queue_flow_stream() PCM chunks
  → ffmpeg encodes PCM → FLAC/MP3 at 1.1× realtime  ← -readrate limits here
    → HTTP resp.write(chunk) → socket → player fetches URL
```

The readrate is set **once** when the ffmpeg encode starts (`get_ffmpeg_stream()`
at controller.py:1015). It applies to the entire flow stream session, which
may span multiple tracks. There is no provider awareness.

**Note:** Single stream (`serve_queue_item_stream()` at line 803-808) has **no**
`-readrate` — it encodes as fast as CPU allows. But single streams are rare;
most players use flow mode.

**Impact:** Sonos/DLNA players receive data at 1.1× realtime, so they can only
be ~3-10s ahead. A network dropout of ~30s exhausts that in seconds.

### Bottleneck 3: Snapcast — `buffered(30)` queue

**File:** `music_assistant/controllers/streams/controller.py:1288-1289`

```python
return buffered(flow_stream, buffer_size=30, min_buffer_before_yield=1)
```

The `buffered()` helper wraps the flow stream in a 30-chunk asyncio.Queue.
This is a small prefetch buffer for the Snapcast TCP source. Snapcast also has
its own configurable `snapcast_server_buffer_size` (200-6000ms).

### Bottleneck 4: AirPlay — `buffered(30)` queue

Same `buffered(30)` path as Snapcast at controller.py:1289. AirPlay also has
its own `airplay_latency` config (250-5000ms) for the RAOP binary.

## Solution

Make **all four bottlenecks** respect a unified three-layer model:

```
Provider says:   "I can supply X seconds ahead"
Player says:     "My hardware can safely hold Y seconds max"
Server computes: target = min(X, Y, ABSOLUTE_MAX)
                 → translates to each player-type's specific mechanism
```

### Design constraints

- `StreamDetails` and `PlayerFeature` live in the external
  `music-assistant-models` pip package — cannot be modified.
- Provider preference flows through MA's `MusicProvider` base class.
- Player cap flows through MA's `Player` base class.
- The translation to specific mechanisms happens per-player-type in their
  respective code paths.

### Layer 1: `MusicProvider.buffer_preference_seconds`

**File:** `music_assistant/models/music_provider.py`

Add property:

```python
@property
def buffer_preference_seconds(self) -> int | None:
    """
    Preferred client-side buffer in seconds for this provider.

    Controls how much encoded audio the server attempts to keep in the
    player's buffer ahead of playback. The server translates this into
    the appropriate mechanism per player type (SendSpin push-ahead limit,
    HTTP ffmpeg readrate, Snapcast/AirPlay buffered queue size).

    * ``None`` (default) — use server default (~30s effective).
      Suitable for streaming providers with legal/business restrictions.
    * ``0`` — no provider-side limit. Allow the player to buffer the
      entire file (subject to the player's hardware cap).
      Suitable for local media, podcasts, audiobooks.
    * ``N`` (>0) — explicit target in seconds.

    Override this in provider subclasses to signal buffering requirements.
    """
    return None
```

| Provider | Override | Rationale |
|---|---|---|
| `filesystem_local` | `0` | Local files have no legal constraints |
| `jellyfin` | `0` | Self-hosted media |
| `plex` | `0` | Self-hosted media |
| `subsonic` | `0` | Self-hosted media |
| All streaming providers | Keep default `None` | No behaviour change |

### Layer 2: `Player.max_client_buffer_seconds`

**File:** `music_assistant/models/player.py`

Add property:

```python
@property
def max_client_buffer_seconds(self) -> int | None:
    """
    Maximum safe client-side buffer in seconds for this player hardware.

    If the device is known to choke on large audio buffers (e.g. Chromecast
    OOM issues), override this to a low value. The server will never exceed
    this cap when computing the effective buffer target, regardless of what
    the provider requests.

    * ``None`` (default) — unknown, use a conservative server default (30s).
    * ``N`` — hard cap in seconds. Server-internal buffers (AudioBuffer) may
      still be larger for seek-back purposes, but the output pacing to this
      player will not exceed N.

    Override this in player provider subclasses to signal hardware limits.
    """
    return None
```

| Player type | Cap | Rationale |
|---|---|---|
| **SendSpin mobile/web** | `300` (5 min) | Modern devices handle this easily |
| **SendSpin Chromecast bridge** | `15` | Cast SDK OOM issues (#3717) |
| **Snapcast** | `300` | Has own server-side buffer config |
| **AirPlay** | `300` | Has own latency config |
| **Sonos** | `None` (default → 30s) | No known issues, conservative |
| **DLNA** | `None` (default → 30s) | No known issues, conservative |

The caps are set by the concrete player provider subclasses (e.g.,
`SendspinPlayer` overrides `max_client_buffer_seconds → 300`,
`chromecast/sendspin_bridge` sets it to 15).

### Layer 3a: SendSpin — Dynamic `_PRODUCER_BUFFER_LIMIT_US`

**File:** `music_assistant/providers/sendspin/playback.py`

#### Step 1: Add buffer target computation

```python
def _compute_buffer_limit_us(
    player: SendspinPlayer,
    media: PlayerMedia,
    mass: MusicAssistant,
) -> int:
    """
    Compute the PushStream buffer limit in microseconds.

    Uses the three-layer model: provider preference → player cap → server absolute max.
    For mixed queues, scans all items and takes the most permissive provider.
    """
    # 1. Provider preference — scan queue items for most permissive
    provider_pref: int | None = None
    if media.source_id and media.queue_item_id:
        queue = mass.player_queues.get(media.source_id)
        if queue is not None:
            for item in queue.items:
                if item.streamdetails:
                    prov = mass.get_provider(item.streamdetails.provider)
                    if prov is not None:
                        pref = getattr(prov, "buffer_preference_seconds", None)
                        if pref == 0:
                            provider_pref = 0
                            break
                        if pref is not None and (provider_pref is None or pref > provider_pref):
                            provider_pref = pref

    # 2. Player cap
    player_cap = player.max_client_buffer_seconds

    # 3. Compute target
    SERVER_DEFAULT_S = 30
    SERVER_ABSOLUTE_MAX_S = 600
    if provider_pref is None:
        target_s = SERVER_DEFAULT_S
    elif provider_pref == 0:
        target_s = SERVER_ABSOLUTE_MAX_S
    else:
        target_s = min(provider_pref, SERVER_ABSOLUTE_MAX_S)
    if player_cap is not None:
        target_s = min(target_s, player_cap)

    return target_s * 1_000_000  # seconds → microseconds
```

#### Step 2: Call at session start

In `_run_playback()` (line 720), after `push_stream = self._create_push_stream()`
and `push_stream.set_live_source(is_live)`, add:

```python
buffer_limit_us = _compute_buffer_limit_us(self.player, media, self.mass)
```

#### Step 3: Replace hardcoded constant usages

| Location | Current | Replacement |
|---|---|---|
| Line 551: `queue_size = (_PRODUCER_BUFFER_LIMIT_US // ...` | Module constant | `buffer_limit_us` |
| Line 865: `await push_stream.sleep_to_limit_buffer(...)` | `_PRODUCER_BUFFER_LIMIT_US` | `buffer_limit_us` |
| Line 1419: `deadline = ... + (_PRODUCER_BUFFER_LIMIT_US / ...` | Module constant | `buffer_limit_us` |

The `_PRODUCER_BACKLOG_SIZE` (64 chunks × 100ms = 6.4s) can remain as-is — it
only bounds the inter-task queue, not the push-ahead limit.

### Layer 3b: HTTP flow — Dynamic ffmpeg readrate

**File:** `music_assistant/controllers/streams/controller.py`

#### Add helper method:

```python
def _calc_flow_readrate(
    self, queue_id: str, player: Player
) -> list[str]:
    """Calculate ffmpeg -readrate args based on queue's most permissive provider."""
    queue = self.mass.player_queues.get(queue_id)
    if not queue:
        return ["-readrate", "1.1", "-readrate_initial_burst", "5"]

    SERVER_DEFAULT_S = 30
    SERVER_ABSOLUTE_MAX_S = 600

    # 1. Scan queue items for the most permissive provider
    provider_pref: int | None = None
    for item in queue.items:
        if item.streamdetails:
            prov = self.mass.get_provider(item.streamdetails.provider)
            if prov is not None:
                pref = getattr(prov, "buffer_preference_seconds", None)
                if pref == 0:
                    provider_pref = 0
                    break
                if pref is not None and (provider_pref is None or pref > provider_pref):
                    provider_pref = pref

    # 2. Player cap
    player_cap = player.max_client_buffer_seconds

    # 3. Compute target
    if provider_pref is None:
        target_s = SERVER_DEFAULT_S
    elif provider_pref == 0:
        target_s = SERVER_ABSOLUTE_MAX_S
    else:
        target_s = min(provider_pref, SERVER_ABSOLUTE_MAX_S)
    if player_cap is not None:
        target_s = min(target_s, player_cap)

    # 4. Translate to readrate: fill target in ~60s wall time
    readrate = max(target_s / 60, 1.1)
    readrate = min(readrate, 10.0)
    burst = int(readrate * 5)

    return ["-readrate", f"{readrate:.1f}", "-readrate_initial_burst", str(burst)]
```

#### Replace hardcoded args at line 1025:

```python
# Before:
extra_input_args=["-readrate", "1.1", "-readrate_initial_burst", "5"],

# After:
extra_input_args=self._calc_flow_readrate(queue_id, player),
```

**Important:** The readrate is fixed for the entire flow stream session
because it's baked into the ffmpeg process at creation time. The queue scan
at session start finds the most permissive provider across all items.
This is safe: streaming items in a mixed queue don't need unlimited rate,
but having the pipeline run faster doesn't harm them.

### Layer 3c: Snapcast & AirPlay — Dynamic `buffered()` queue size

**File:** `music_assistant/controllers/streams/controller.py`

In `get_stream()` at line 1288-1289:

```python
# Before:
if use_flow_stream_buffering:
    return buffered(flow_stream, buffer_size=30, min_buffer_before_yield=1)

# After:
if use_flow_stream_buffering:
    buf_seconds = self._calc_buffered_queue_size(queue, player)
    # buffered() uses chunk count; at 1 chunk ≈ 1 PCM second
    return buffered(flow_stream, buffer_size=buf_seconds, min_buffer_before_yield=1)
```

Helper:

```python
def _calc_buffered_queue_size(
    self, queue: PlayerQueue | None, player: Player
) -> int:
    """Compute buffered() queue size from provider preference + player cap."""
    SERVER_DEFAULT_S = 30
    SERVER_ABS_MAX_S = 120  # 2 min max for in-process queue

    provider_pref: int | None = None
    if queue is not None:
        for item in queue.items:
            if item.streamdetails:
                prov = self.mass.get_provider(item.streamdetails.provider)
                if prov is not None:
                    pref = getattr(prov, "buffer_preference_seconds", None)
                    if pref == 0:
                        provider_pref = 0
                        break
                    if pref is not None and (provider_pref is None or pref > provider_pref):
                        provider_pref = pref

    player_cap = player.max_client_buffer_seconds

    if provider_pref is None:
        target_s = SERVER_DEFAULT_S
    elif provider_pref == 0:
        target_s = SERVER_ABS_MAX_S
    else:
        target_s = min(provider_pref, SERVER_ABS_MAX_S)
    if player_cap is not None:
        target_s = min(target_s, player_cap)

    return max(target_s, 15)  # at least 15s
```

### Layer 3d: AudioBuffer capacity

**File:** `music_assistant/controllers/streams/audio_buffer.py`

The server-side AudioBuffer (PCM in memory) currently uses system RAM-based
presets (60/300/1200s). When the output readrate increases, the AudioBuffer
must have enough PCM to feed ffmpeg. Adjust its capacity in `get_buffer()`
(line 352-490) to match the provider preference:

```python
# After constructing AudioBuffer (line 453), before fill() (line 478):
if mode == BufferMode.SEEKABLE:
    pref: int | None = None
    provider_obj = mass.get_provider(streamdetails.provider)
    if provider_obj is not None:
        pref = getattr(provider_obj, "buffer_preference_seconds", None)
    if pref == 0:
        audio_buffer.max_size_seconds = min(
            streamdetails.duration or BUFFER_SIZE_MAP[BufferSize.MAXIMUM],
            BUFFER_SIZE_MAP[BufferSize.MAXIMUM],
        )
    elif pref is not None and pref > 0:
        audio_buffer.max_size_seconds = min(
            pref, BUFFER_SIZE_MAP[BufferSize.MAXIMUM]
        )
    # Re-cap ready_threshold to new max_size
    audio_buffer._ready_threshold = min(
        audio_buffer._ready_threshold, audio_buffer.max_size_seconds
    )
```

### Implementation order

| Step | File | Change |
|------|------|--------|
| 1 | `music_assistant/models/music_provider.py` | Add `buffer_preference_seconds` property (default `None`) |
| 2 | `music_assistant/models/player.py` | Add `max_client_buffer_seconds` property (default `None`) |
| 3 | `music_assistant/controllers/streams/controller.py` | Add `_calc_flow_readrate()` + `_calc_buffered_queue_size()`; modify lines 1025 and 1289 |
| 4 | `music_assistant/controllers/streams/audio_buffer.py` | Apply provider preference to AudioBuffer capacity |
| 5 | `music_assistant/providers/sendspin/playback.py` | Add `_compute_buffer_limit_us()`; replace `_PRODUCER_BUFFER_LIMIT_US` at lines 551, 865, 1419 |
| 6 | `music_assistant/providers/filesystem_local/__init__.py` | Override `buffer_preference_seconds → 0` |
| 7 | `music_assistant/providers/jellyfin/__init__.py` | Override `buffer_preference_seconds → 0` |
| 8 | `music_assistant/providers/plex/__init__.py` | Override `buffer_preference_seconds → 0` |
| 9 | `music_assistant/providers/subsonic/__init__.py` | Override `buffer_preference_seconds → 0` |
| 10 | `music_assistant/providers/sendspin/player.py` | Override `max_client_buffer_seconds → 300` |
| 11 | `music_assistant/providers/snapcast/player.py` | Override `max_client_buffer_seconds → 300` |
| 12 | `music_assistant/providers/airplay/player.py` | Override `max_client_buffer_seconds → 300` |
| 13 | `music_assistant/providers/chromecast/sendspin_bridge.py` | Override `max_client_buffer_seconds → 15` |

### Mixed queue scenario: how it works

Consider a queue: **Spotify track → local FLAC → Spotify track**

**At session start** (both HTTP flow and SendSpin):

1. Scan `queue.items` for provider preferences:
   - Spotify → `None` (default 30s)
   - filesystem_local → `0` (unlimited)
   - Spotify → `None` (default 30s)
2. Most permissive wins: `0` (unlimited)
3. Apply player cap (e.g., Sonos `None` → cap at 600s, or Chromecast `15` → cap at 15s)
4. If player cap is 600s:
   - **HTTP flow:** `-readrate = min(600/60, 10) = 10.0`
   - **SendSpin:** `_PRODUCER_BUFFER_LIMIT_US = 600_000_000`
5. Throughout the queue, audio is pushed at high speed. The Spotify items don't
   need it, but they don't suffer from it either — the stream is continuous.
6. When the queue ends or the user skips, the session restarts with new
   computation.

### Example scenarios

| Provider | Player | target_s | HTTP readrate | SendSpin limit | Effect |
|---|---|---|---|---|---|
| Spotify | Sonos | 30 (default) | 1.1× | 30s | Same as today |
| Spotify | Chromecast | 15 (player cap) | 1.1× | 15s | Slightly less than today (safe) |
| Local file | Sonos | 600 (abs max) | 10.0× | 600s | 10 min buffered in ~60s |
| Local file | Chromecast | 15 (player cap) | 1.1× | 15s | Chromecast protected from OOM |
| Local file | SendSpin mobile | 300 (player cap) | N/A | 300s | 5 min buffered via WebSocket |
| Podcast | SendSpin mobile | 300 (player cap) | N/A | 300s | Survives 5 min dropout |

### Risks

| Risk | Mitigation |
|---|---|
| **Chromecast OOM** with large buffer | Chromecast player cap = 15s, enforced regardless of provider preference |
| **SendSpin mobile OOM** with 300s buffer | 300s of Opus @ 160kbps = ~6MB. Manageable on modern phones. WebSocket has backpressure. |
| **Skip/next latency** with large client buffer | On `play_index()`, the flow stream session is cancelled and a new one starts. The player receives a fresh URL and discards its old buffer. |
| **Mixed queue readrate mismatch** | Most permissive provider wins for the entire session. Streaming items between local files are just pushed faster than needed — no data loss. |
| **ffmpeg CPU overload** at readrate 10× | FLAC encode at 44100/16/2 runs at ~50× on modern hardware. readrate=10 is easily sustained. |

### Open questions

- **Provider changes mid-queue after session start:** If the user adds a
  streaming provider item after the session started, the scan at session start
  already accounted for it (all items are known). If the user removes the
  local file item, the session continues at the previously computed rate —
  the session would need to be restarted to re-scan. This is acceptable:
  queue edits already restart the session.

- **Single stream readrate:** Single streams (`serve_queue_item_stream()`)
  have no readrate limit today. Should we add one based on the same model?
  Single streams are used for radio and non-flow players, where buffering is
  less of a concern. Leave them unlimited for now.

- **SendSpin `_PRODUCER_BACKLOG_SIZE`:** This 64-chunk (6.4s) inter-task queue
  is separate from the 30s push-ahead limit. It bounds memory of the
  producer-consumer pipeline, not the client buffer. Keep as-is.

### Tests

| Test | Scope |
|---|---|
| `test_provider_pref_default` | `None` → target = 30s default |
| `test_provider_pref_unlimited` | `0` → target = min(600, player_cap) |
| `test_provider_pref_explicit` | `120` → target = min(120, player_cap) |
| `test_player_cap_limits_provider` | Chromecast cap 15s limits even unlimited provider |
| `test_mixed_queue_most_permissive` | Queue with Spotify + local file → uses unlimited |
| `test_http_readrate_calc` | target→readrate conversion matches formula |
| `test_sendspin_buffer_limit_us` | `_compute_buffer_limit_us` returns correct µs |
| `test_buffered_queue_size` | `_calc_buffered_queue_size` returns correct chunks |
| `test_audio_buffer_capacity` | AudioBuffer adjusts max_size_seconds from provider pref |
| `test_filesystem_local_override` | `buffer_preference_seconds` returns `0` |
| `test_sendspin_player_cap` | `max_client_buffer_seconds` returns `300` |
| `test_chromecast_bridge_cap` | Chromecast bridge returns `15` |

### Acceptance criteria

- [x] Analysis and implementation plan documented as markdown, with Mermaid
      diagrams where it helps
- [ ] Unit tests for each change created and successful
- [ ] HTTP flow: local files on Sonos/DLNA survive >60s network dropout
- [ ] SendSpin: local files on mobile app survive >60s network dropout
- [ ] Chromecast: buffer never exceeds 15s cap (regression test)
- [ ] Mixed queue: works without interruption at the highest permitted rate

### Future work

- **Multiple files ahead pre-buffering:** Once a single file can be pushed at
  high speed, extend `prepare_next_audio_buffer()` (triggered at duration-60s)
  to start earlier for local files.
- **Dynamic mid-session re-evaluation:** If the queue changes dramatically
  after session start, restart the session to re-scan provider preferences.
- **User-facing config:** Expose `max_client_buffer_seconds` as a per-player
  setting in the UI for advanced users.
