# WLED Audio Sync Plugin Provider

A Music Assistant **Plugin Provider** that drives WLED installations with the **AudioReactive** usermod in real time. It connects to MA's local Sendspin server as a `VISUALIZER` client, receives pre-computed visualizer frames (loudness, dominant peak frequency, 16-bin log spectrum), maps them onto the **WLED V2 audio-sync** UDP wire format, and fans them out to one or many WLED receivers. This includes recent upstream WLED (v16.0.0+) and the MoonModules fork.

Mirrors the design of `hue_entertainment/`: one provider, N internal "bridges" (one per WLED destination), and the user-facing player surface is owned by Sendspin via `register_bridge_player_type(client_id, PlayerType.VISUALIZER)`.

## Clean-room declaration

> This implementation is derived solely from (a) a packet capture taken by the author from a device running WLED v16.0.0 on the author's own LAN, and (b) the protocol *facts* (field names, byte offsets, default network parameters) described in the public WLED V2 audio-sync documentation at <https://mm.kno.wled.ge/WLEDSR/UDP-Sound-Sync/>. **No source code has been consulted from any of the following projects**, all of which are copyleft-licensed and would taint an Apache-2.0 clean-room derivation:
>
> - `wled/WLED` (EUPL-1.2 as of v16; relicensed from MIT).
> - `MoonModules/WLED-MM` (EUPL-1.2) — including the `usermods/audioreactive/` directory.
> - `chrisgott/feed_my_wled` (GPL-3.0).
> - `Victoare/SR-WLED-audio-server-win` (GPL-3.0).

Music Assistant is Apache-2.0, which is incompatible with both GPL-3.0 and EUPL-1.2 for direct code reuse. The wiki page above is *documentation* describing an on-wire binary protocol; the byte layout, struct field names, and default network parameters are facts about a binary protocol rather than copyrightable creative expression. The pcap is the author's own capture of network traffic on the author's own LAN, with no copyrighted firmware code involved.

If a protocol detail isn't covered by the wiki or by what can be observed in the capture, the correct action is to **stop and ask** — not to read the WLED or WLED-MM firmware source.

## Scope

### In scope (v1)

- Auto-discovery of WLED devices over zeroconf (`_wled._tcp.local.`), filtered to AudioReactive-capable builds via `/json/info`.
- One internal **bridge** per discovered device, plus manually-configured bridges for broadcast / multicast destinations that don't appear in mDNS.
- Each bridge is a `Roles.VISUALIZER` Sendspin client; the Sendspin provider exposes those clients to the user as `PlayerType.VISUALIZER` sync-group-only "lights" players.
- Analyzer mapping Sendspin `VisualizerFrame`s (loudness / f_peak / 16-bin spectrum) → WLED V2 frame fields, with global AGC, exponential smoothing for sampleSmth, and rolling-stats beat detection for samplePeak.
- V2 packet emission (unicast, broadcast, or multicast) at the natural Sendspin visualizer rate (~43 Hz), latency-compensated via `compute_play_time` and `loop.call_later`.
- Provider-level duplicate-transmit toggle and multicast-TTL override.

### Out of scope (v1)

- WLED control beyond audio sync (colors, effects, presets, segments — that's WLED's HTTP/JSON API).
- Non-V2 protocols (V1 audio sync, ESPNOW, native WLED audio streaming).
- Rendering audio-reactive visuals on the MA server itself (Sendspin already publishes a generic visualizer feed; this provider only consumes it).
- An HTTP API into MA from a WLED — strictly one-way (MA → WLED).

## Architecture

```
                                  ┌──────────────────────────────────────┐
   MA queue                       │  WLED Audio Sync Plugin Provider     │
       │                          │                                       │
       ▼                          │   ┌─────────────────────────────┐    │
   audible Player                 │   │  mDNS browser (provided by  │    │
   (AirPlay/Sonos/etc.)           │   │  mass.discovery.aiozc)      │    │
                                  │   └─────────────────────────────┘    │
                                  │              │                         │
                                  │              ▼ on_mdns_service_*       │
                                  │   ┌─────────────────────────────┐    │
                                  │   │  /json/info probe           │    │
                                  │   │  → AudioReactive present?   │    │
                                  │   └─────────────────────────────┘    │
                                  │              │                         │
                                  │              ▼                         │
                                  │       WledAudioSyncBridge              │
                                  │       (one per WLED destination)       │
                                  │              │                         │
                                  │              │ SendspinClient(VISUALIZER)│
                                  │              ▼                         │
   ┌─────────────────────┐        │   ┌─────────────────────────────┐    │
   │ Sendspin provider   │ frames │   │  _on_visualizer_frames      │    │
   │ (local server,      │ ──────►│   │   compute_play_time +       │    │
   │ does PCM + FFT)     │  WS    │   │   loop.call_later schedule  │    │
   └─────────────────────┘        │   └─────────────────────────────┘    │
                                  │              │                         │
                                  │              ▼                         │
                                  │   ┌─────────────────────────────┐    │
                                  │   │  WledAudioAnalyzer          │    │
                                  │   │   → WledV2Frame             │    │
                                  │   └─────────────────────────────┘    │
                                  │              │                         │
                                  │              ▼                         │
                                  │   ┌─────────────────────────────┐    │
                                  │   │  encode_v2(frame) → 44 B    │    │
                                  │   └─────────────────────────────┘    │
                                  │              │                         │
                                  │              ▼                         │
                                  │   ┌─────────────────────────────┐    │
                                  │   │  WledV2Transport.send()     │    │
                                  │   │  asyncio DatagramTransport  │    │
                                  │   └─────────────────────────────┘    │
                                  └──────────────┬───────────────────────┘
                                                 │ UDP :11988
                                                 ▼ (×2 if duplicate-tx on)
                                         ┌───────┴───────┐
                                         │ WLED ESP32     │ (unicast / broadcast / multicast)
                                         └───────────────┘
```

## File map

```
wled_audiosync/
├── __init__.py     - provider setup() + provider-level config entries
├── bridge.py       - WledAudioSyncBridge (one per WLED destination; owns the
│                     Sendspin client, the analyzer, and the UDP transport)
├── constants.py    - MA-side config keys + defaults (re-exports the bridge's
│                     protocol constants for convenience)
├── manifest.json   - type=plugin, mdns_discovery=["_wled._tcp.local."],
│                     multi_instance=false
├── provider.py     - WledAudioSyncProvider (PluginProvider) +
│                     info_has_audioreactive + probe_audioreactive
└── wled_audiosync_bridge/   - intentionally MA-decoupled "extractable" sibling subpackage
    ├── __init__.py - re-exports the public surface
    ├── analyzer.py - WledAudioAnalyzer (VisualizerFrame → WledV2Frame)
    ├── constants.py - protocol constants (magic header, port, packet size,
    │                   multicast group)
    ├── encoder.py  - encode_v2() packing a frame to 44 bytes
    └── transport.py - WledV2Transport (asyncio DatagramTransport + error /
                       reset handling)
```

The `wled_audiosync_bridge/` layout mirrors `hue_entertainment/hue_sendspin_bridge/` — the bridge subpackage contains the building blocks that don't need anything from `music_assistant.*`, so it can later be extracted as a standalone library if useful. The only external runtime dependency is `aiosendspin` (for the `VisualizerFrame` type).

## Discovery

`manifest.json` declares `mdns_discovery: ["_wled._tcp.local."]`. MA's discovery controller registers a single global zeroconf browser over every provider's declared service types and invokes `Provider.on_mdns_service_state_change(name, state_change, info)` on add/update/remove events.

On `Added` / `Updated`:

1. Extract the device's IPv4 address from `info.addresses` via `get_primary_ip_address_from_zeroconf`.
2. If `require_audioreactive=True` (default), probe `http://<addr>/json/info` (5 s timeout, MA-wide aiohttp session).
3. If the response contains `"AudioReactive"` in the `u` (usermods) dict, register a `WledAudioSyncBridge`. Otherwise skip — the device can still be retried on the next mDNS event.

On `Removed`: stop the existing bridge and drop it from the registry.

**`info.port` is the HTTP UI port (typically 80), NOT the V2 audio-sync RX port.** All discovered bridges are pinned to `WLED_AUDIOSYNC_DEFAULT_PORT = 11988`.

## Configuration

All config is provider-level (one provider, N internal bridges — there's no per-Player config because the player surface lives in Sendspin).

| Key | Type | Default | Description |
|---|---|---|---|
| `manual_players` | `STRING` multi-value | `[]` | Targets that don't show up in mDNS — typically broadcast/multicast endpoints. One entry per line, `'<friendly name>=<address>'`. Multicast addresses (e.g. `239.0.0.1`) are detected automatically. |
| `require_audioreactive` | `BOOLEAN` (advanced) | `true` | When enabled, only discovered WLEDs whose `/json/info` reports the AudioReactive usermod are bridged. Disable to bridge every WLED for diagnostics — non-MM devices simply ignore V2 packets. |
| `duplicate_transmit` | `BOOLEAN` (advanced) | `true` | Send each V2 packet twice back-to-back. Mirrors the firmware capture behaviour and improves resilience against single-packet loss. Applies to every bridge. |
| `multicast_ttl` | `INTEGER` (advanced, 1-255) | `1` | IP TTL for outgoing multicast packets. Only takes effect on bridges whose destination is a multicast group. |

## Protocol — V2 audio-sync UDP packet

Validated byte-for-byte against a real-hardware capture from an ESP32 running upstream WLED v16.0.0 with the AudioReactive usermod. The wiki documents the field semantics; the **byte layout below is the on-wire ground truth**.

### Network

| | |
|---|---|
| Transport | UDP, IPv4 |
| Source port | `11988` (observed) |
| Destination port | `11988` (configurable on the WLED side) |
| Default destination | Multicast group `239.0.0.1` (observed). Unicast and broadcast also valid. |
| Redundancy | Each distinct frame is transmitted **twice** back-to-back (~33 µs apart) for receiver-side loss resilience. |
| Unique-frame cadence | ~42.7 Hz (median 23.35 ms between distinct frames). |
| UDP payload | 44 bytes |

### Payload — 44 bytes, little-endian, naturally aligned

| Offset | Size | Field | Type | Notes |
|--------|------|-------|------|-------|
| `0x00` | 6 | `header` | `char[6]` | Magic, always `b"00002\x00"`. |
| `0x06` | 2 | _pad | — | Compiler-inserted alignment padding. **Encoder writes zero.** |
| `0x08` | 4 | `sampleRaw` | `float32 LE` | AGC-scaled audio amplitude. Capture range 0.0–254.0 (0-255 scale, NOT raw int16). |
| `0x0C` | 4 | `sampleSmth` | `float32 LE` | Smoothed/averaged amplitude. Capture range 0.0–255.0. |
| `0x10` | 1 | `samplePeak` | `uint8` | Beat-detected flag — capture observed only `{0, 1}`. |
| `0x11` | 1 | `reserved1` | `uint8` | Always `0` in capture. |
| `0x12` | 16 | `fftResult[16]` | `uint8[16]` | 16 log-spaced GEQ bands, magnitude 0-255 each. |
| `0x22` | 2 | _pad | — | Compiler-inserted alignment padding. **Encoder writes zero.** |
| `0x24` | 4 | `FFT_Magnitude` | `float32 LE` | Magnitude of dominant FFT peak. Capture range 0.0–20685. |
| `0x28` | 4 | `FFT_MajorPeak` | `float32 LE` | Dominant peak frequency in Hz. Capture range 44.5–1149 Hz. |

Pack with `struct.pack("<6s2sff2B16s2sff", ...)` — yields exactly 44 bytes. The encoder is in `encoder.py`; a byte-golden roundtrip test (`test_encoder.py::test_encoder_matches_real_capture_byte_for_byte`) asserts the encoder reproduces a captured packet exactly when given the captured field values.

### Why 44 bytes and not 40 (wiki value)

The MoonModules wiki documents the struct field bytes as summing to 40 (6+4+4+1+1+16+4+4). The actual on-wire payload is **44 bytes** because of two natural-alignment padding regions: 2 bytes after `header[6]` (to align the next `float`) and 2 bytes after `fftResult[16]` (to align `FFT_Magnitude`). Both regions are zero in every observed packet; the encoder writes them as zero.

## Analyzer — `VisualizerFrame` → `WledV2Frame`

The bridge does no FFT of its own. Sendspin already runs a shared analysis pipeline that produces a `VisualizerFrame` per audio window with the fields any visualizer needs (`loudness: uint16`, `f_peak: int`, `spectrum: list[int]` of arbitrary length, log- or linear-spaced per the client's `ClientHelloVisualizerSpectrum` request). The bridge requests a **16-bin log spectrum spanning 40 Hz – 10 kHz at 43 Hz max rate** from Sendspin, so each `VisualizerFrame` already matches the V2 GEQ shape one-for-one.

`WledAudioAnalyzer.process_frame(frame)` maps each `VisualizerFrame` onto a `WledV2Frame`:

| WLED V2 field | Source | Notes |
|---|---|---|
| `sample_raw` | `loudness / 65535 * 255` | Scale uint16 loudness into the 0-255 amplitude range observed on the wire. |
| `sample_smth` | EMA over `sample_raw` (α=0.3) | Exponential smoothing for less flickery visuals. |
| `sample_peak` | rolling-stats beat detect | `samplePeak=1` when `sample_raw > rolling_mean + N·rolling_stddev` (N=1.5, window=16 frames, min 4 frames warm-up). |
| `fft_bands[16]` | global-AGC normalise `frame.spectrum` | One envelope across all 16 bands, exponential release ~1 s. **Not per-band** — see "Implementation notes" below. |
| `fft_magnitude` | `max(frame.spectrum)` | Proxy for dominant-peak magnitude (Sendspin doesn't expose an absolute magnitude; receivers treat it as relative anyway). |
| `fft_major_peak_hz` | `frame.f_peak` | Verbatim pass-through. |

The analyzer holds three pieces of stateful smoothing per bridge: the AGC envelope, the sample_smth EMA value, and the rolling-history `deque` for sample_peak. A fresh analyzer is built every time the bridge starts.

## Sendspin VISUALIZER consumption

`WledAudioSyncBridge` connects to MA's local Sendspin server at `ws://<bind_ip>:8927/sendspin` as a `Roles.VISUALIZER` client with:

```python
visualizer_support = ClientHelloVisualizerSupport(
    buffer_capacity=100,
    types=["loudness", "f_peak", "spectrum"],
    batch_max=1,
    spectrum=ClientHelloVisualizerSpectrum(
        n_disp_bins=16,
        scale="log",
        f_min=40,
        f_max=10000,
        rate_max=43,
    ),
)
```

Sendspin delivers frames ~100-200 ms before their audible play time, each carrying the original server-side `timestamp_us`. The bridge converts each timestamp into a local monotonic deadline via `SendspinClient.compute_play_time(...)` and schedules the UDP send with `loop.call_later`. Frames whose deadline has already passed by more than 50 ms are dropped rather than emitted visibly late.

`stream/start` / `stream/end` events drive a small streaming-state flag with a 2 s debounce on stream-end so track transitions don't flicker the strip dark.

## Implementation notes worth recording

Architectural decisions made during implementation that aren't obvious from reading the code:

- **Global AGC, not per-band.** The first cut normalised each GEQ band against its own running max. Spectrum leakage at -32 dB still produced enough magnitude in neighbour bands that their independent envelopes scaled them to 255 — PS GEQ 1D on hardware showed a flat top-of-strip bar regardless of input. Replaced with a single global envelope tracking the loudest band's amplitude. The reference firmware capture confirms relative band magnitudes survive normalisation on the wire (e.g. `[159, 120, 94, 151, 113, 83, 181, 230, 220, 72, ...]`), not per-band saturation.
- **mDNS port is HTTP, not audio-sync.** WLED's `_wled._tcp.local.` mDNS record advertises the device's HTTP UI port (typically 80). The V2 audio-sync RX port (default `11988`) is configured separately on the WLED. The provider pins all discovered bridges to `WLED_AUDIOSYNC_DEFAULT_PORT = 11988`.
- **Sender-side `IP_ADD_MEMBERSHIP` for multicast.** A multicast sender doesn't strictly need group membership to send, but joining the group keeps IGMP-snooping switches' multicast-forwarding tables refreshed during long-running playback. The kernel handles the periodic IGMP report cadence automatically.
- **TransportSocket gotcha.** `asyncio.DatagramTransport.get_extra_info("socket")` returns an `asyncio.trsock.TransportSocket` wrapper, not a raw `socket.socket`. `isinstance(_, socket.socket)` returns False on it. The transport's `socket` property declares a structural `SocketLike` Protocol for the surface we actually use.
- **Auto-reset + `/json/info` re-probe.** UDP normally swallows sends to offline hosts silently. The transport tracks consecutive sendto failures (rate-limited log; auto-reset after a 300-failure threshold) and fires an `on_reset` callback. The bridge wires that callback to a `/json/info` probe so users get a single useful "device offline" warning instead of either silence or a flood. Multicast / broadcast destinations skip the probe (no HTTP behind them).
- **AudioReactive detection by usermod presence.** The MM firmware fork keeps `info["brand"] == "WLED"` (same as vanilla). The differentiator is the presence of `"AudioReactive"` in the top-level `u` (usermods) dict of `/json/info`. `info_has_audioreactive()` and `probe_audioreactive()` cover both the synchronous check and the HTTP-with-graceful-failure flow.
- **Sendspin owns the player surface.** The bridge calls `sendspin_provider.register_bridge_player_type(client_id, PlayerType.VISUALIZER)` on start so the Sendspin provider exposes the client as a sync-group-only "lights" player. This provider never calls `mass.players.register`. Mirrors `hue_entertainment.bridge.HueEntertainmentBridge`.

## Tests

72 tests across 6 files under `tests/providers/wled_audiosync/`. Run with:

```
pytest --no-cov tests/providers/wled_audiosync/
```

| Test file | Count | Focus |
|---|---:|---|
| `test_encoder.py` | 6 | Byte-golden roundtrip against the pcap, padding invariants, format guards. |
| `test_analyzer.py` | 17 | `VisualizerFrame` → `WledV2Frame` mapping: spectrum padding/truncation, loudness scaling, sample_smth EMA, global-AGC normalisation, f_peak pass-through, rolling beat detection, None-spectrum guard. |
| `test_transport.py` | 21 | Classifier, unicast/broadcast/multicast options, duplicate-tx, error counter + log throttle + auto-reset, `on_reset` firing. |
| `test_provider.py` | 23 | AudioReactive detection (pure + HTTP probe), manual-bridge registration, `on_mdns_service_state_change` for all event types. |
| `test_integration.py` | 5 | End-to-end with a real loopback UDP listener — synthetic `VisualizerFrame` in, decode the 44-byte packets out. |

## Hardware-in-loop verification

Manual procedure for verifying frequency mapping on real LEDs:

1. WLED device on the LAN with AudioReactive usermod enabled and UDP Sound Sync RX configured for the V2 protocol on port `11988`.
2. In WLED, select the **PS GEQ 1D** effect (each of the 16 LED columns then maps directly to one of our `fft_bands` values).
3. In MA, add the WLED Audio Sync provider (Settings → Providers → Add Provider). The WLED device should auto-discover; manually-configured broadcast / multicast bridges appear alongside as Sendspin visualizer players.
4. Add the WLED player to a sync group with whichever audible Player you want it to react to.
5. Play one of the standard test signals — a 20 Hz → 20 kHz log sweep, octave-spaced tone bursts (50 Hz, 100 Hz, …, 10 kHz), pink noise, or white noise. Expected behaviour per signal:
   - **Sweep**: a single pillar of activity walks smoothly left → right across the strip over 30 s.
   - **Tones**: 9 discrete pulses, each lighting ~1 column; successive tones advance ~2 columns per octave.
   - **Pink noise**: all 16 columns at roughly equal height.
   - **White noise**: columns rise from short on the left to tall on the right (more linear-Hz energy per upper log-band).

## Open design questions for maintainer review

Carried forward into the eventual PR for upstream sign-off:

- **Pacing helper.** `compute_play_time` + `loop.call_later` is the same pacing pattern Hue Entertainment uses. Hardware testing should confirm latency is acceptable across track boundaries and sync-group changes; if not, expose a per-bridge `latency_us` config (already plumbed into `WledAudioSyncBridge.__init__` for tuning).
- **Group-sync wall-clock alignment.** When a sync group plays the same track to AirPlay + a WLED player, the Sendspin `timestamp_us` is the canonical reference for "when this audio is audible". Confirm that AirPlay's own latency falls within the same envelope on real hardware; if not, document the per-Player visual-offset slider as a follow-up.
- **Capture mismatch on `fftResult` bands 10-15.** In the reference capture, only bands 0-9 carry data; 10-15 are zero. Is this because (a) the firmware build sampled exposes only 10 bands, (b) the audio content didn't excite the upper bands, or (c) the protocol implicitly reserves trailing bands? Confirm against MM source. v1 fills all 16; if receivers ignore 10-15, no harm.
- **`sampleRaw` / `sampleSmth` scale.** Capture confirms 0-255 range, not raw int16. Confirm the exact AGC math the firmware sender applies so MA-side AGC produces visually equivalent results.
