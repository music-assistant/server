# Local Audio Out Provider

## Overview

The Local Audio Out provider exposes locally attached soundcards as players in Music Assistant. On Linux it supports two backends: **PulseAudio/PipeWire** (enumerates PA sinks — USB DACs, built-in audio, HDMI, remap sinks, virtual sinks, etc.) and **ALSA direct** (enumerates hardware `hw:` devices via PortAudio). On macOS it enumerates CoreAudio devices via PortAudio. It leverages the Sendspin provider for synchronization and timing, registering each device as an external Sendspin bridge client.

### Key Features

- **Automatic Device Discovery**: On Linux with PulseAudio/PipeWire, enumerates all output sinks via `pactl --format=json` — returns native sample rate and format regardless of active stream state. On Linux with ALSA direct, enumerates hardware `hw:` devices via PortAudio. On macOS, enumerates via PortAudio/sounddevice
- **Backend Selector** *(Linux)*: Choose between Auto (PulseAudio/PipeWire if available, else ALSA direct), PulseAudio/PipeWire, or ALSA direct. Auto mode detects PulseAudio/PipeWire first and falls back to ALSA if unavailable
- **Native Format Negotiation** *(Linux PulseAudio)*: Each PA sink advertises its native sample rate and bit depth (16, 24, or 32-bit) so Music Assistant transcodes to the correct format — no unnecessary resampling
- **Sendspin Integration**: Each device is registered as a regular, visible MA player whose (single) output protocol is provided by a Sendspin bridge client, enabling synchronized multi-room playback. Disabling the player tears the bridge down; enabling it re-registers the player and rebuilds the bridge
- **Self-Managed Remap-Sink Topology** *(Linux PulseAudio)*: For multi-channel sound cards (5.1, 7.1), the provider creates and owns its own `module-remap-sink` topology on startup — one stereo "zone" sink per channel pair (front, rear, side, center/LFE) plus a full-channel "multichannel stereo" passthrough sink. No external addon or pre-configuration is required; the topology is created idempotently on every startup and torn down on provider stop
- **Hardware Volume Control** *(Linux PulseAudio)*: Per-player volume and mute are applied as native PulseAudio sink volume via `libpulse`, using an exponential "audio taper" curve mapped from the MA 0-100 slider so slider position corresponds to a constant dB change per step (see Volume Control below). Each remap sink has an independent hardware volume that doesn't affect its master sink or sibling sinks. Falls back to software volume (PCM scaling) if hardware volume control is unavailable
- **Stable Player IDs**: Uses UUIDv5 derived from device name + host API index so players persist across restarts
- **Volume State Persistence**: Volume level is cached and restored on restart. Mute state is intentionally *not* restored on restart — a player never starts up silently muted
- **PA Stream Pre-Warming** *(Linux PulseAudio)*: PA streams are opened at provider startup rather than at first play, eliminating the cold-start stream-open latency that causes a fixed sync offset when multiple bridges open their streams simultaneously on the first play in a sync group

## Architecture

### Component Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    LocalAudioProvider                         │
│  - Thin provider shell, delegates to bridge manager          │
│  - Verifies libpulse-simple present on Linux (PA backend)    │
└──────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────▼──────────────┐
                │  LocalAudioBridgeManager   │
                │  (SendspinBridgeManagerBase│
                │   desired-state reconciler)│
                │  - Enumerates devices      │
                │  - Registers device players│
                │  - Creates/stops bridges   │
                └─────────────┬──────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                                       │
┌─────────▼──────────┐              ┌─────────────▼──────────┐
│ SendspinLocalAudio  │              │ SendspinLocalAudio     │
│ Bridge (Device A)   │              │ Bridge (Device B)      │
│                     │              │                        │
│ Sendspin Client ──► │              │ Sendspin Client ──►    │
│ BridgePlayerRole    │              │ BridgePlayerRole       │
│ PA/ALSA/sounddevice │              │ PA/ALSA/sounddevice    │
└─────────────────────┘              └────────────────────────┘
```

### Audio Flow

#### Linux (PulseAudio / PipeWire)
```
Sendspin PushStream
       │
       ▼
BridgePlayerRole.on_audio_chunk
       │
       ▼ (format conversion for 24-bit; software volume/mute applied
       │  only if hardware volume control unavailable)
asyncio.Queue
       │
       ▼
PASimpleStream (libpulse-simple via ctypes)
│  Pre-warmed at provider startup — stream-open latency paid once
│  during init, not at first play, so sync group timing is tight
       │
       ▼
PulseAudio Sink (hardware volume set via PAVolumeController —
       │           independent per-sink, audio-taper mapped from
       │           MA volume slider; master sink pinned at 100%
       │           so remap sinks are never doubly attenuated)
       ▼
Physical Audio Device / remap-sink master
```

#### Linux (ALSA direct) / macOS (CoreAudio)
```
Sendspin PushStream
       │
       ▼
BridgePlayerRole.on_audio_chunk
       │
       ▼ (software volume/mute applied)
asyncio.Queue
       │
       ▼
sounddevice.RawOutputStream (PortAudio, int16)
       │
       ▼
ALSA hw: device / CoreAudio Device
```

### Volume Control

The MA volume slider (0-100) is mapped to an amplitude scale factor via an exponential "audio taper" curve (`volume_pct_to_amplitude` in `constants.py`, based on the [dr-lex taper](https://www.dr-lex.be/info-stuff/volumecontrols.html)): a constant dB change per slider step from 10% to 100%, with a linear ramp to true silence below 10%. This avoids the classic "linear volume slider" problem where the bottom of the range is wildly more sensitive than the top and the top barely changes the loudness at all. The same taper is used for both the hardware and software volume paths below, so a given slider position sounds the same regardless of which path is active.

The taper's dynamic range is configurable via `_TAPER_A` in `constants.py` — the default is **40dB** (`_TAPER_A = 0.01`), suited for receiver-driven and outdoor speaker setups (MA 70% ≈ -12dB). A 60dB range (`_TAPER_A = 0.001`) is more appropriate for headphones or desktop speakers. See `constants.py` for a reference table of common options.

**Linux PulseAudio/PipeWire backend**: Volume and mute are applied as native PulseAudio sink volume, via a shared `libpulse` connection (`PAVolumeController`). The taper's output amplitude is converted to a PA volume percentage via a cube root — PA's own volume percentage represents `amplitude**3` on its internal cubic curve, so the cube root is the inverse step needed to make PA apply the *taper's* amplitude rather than its own.

Each sink — including each `module-remap-sink` zone sink on multi-channel cards — has its own independent hardware volume that does not affect its master sink or sibling sinks. This means each zone of a 5.1/7.1 card (front, rear, side, center/LFE, and the full-channel "multichannel stereo" passthrough) gets its own independent volume control in MA.

Cached volume *level* is restored on restart so the MA slider position persists. Mute state is **not** restored on restart — regardless of how a player was left muted in a previous session, it always starts unmuted, so a stale mute can never silently prevent audio after a restart.

If hardware volume control is unavailable (e.g. `libpulse` connection fails), the bridge falls back to software volume control — PCM samples are scaled (using the same taper-derived amplitude) before being written to the output device, with the underlying PA sink volume left at its default.

**ALSA direct backend (Linux) and macOS**: Volume and mute are controlled entirely in software — PCM samples are scaled by the taper-derived amplitude in the bridge before being written to the output device. Hardware mixer levels are not controlled by MA and must be configured by the user once using a tool such as `alsamixer`. Set all relevant controls to 100% and save the state with `alsactl store` so the levels persist across reboots.

This approach gives consistent behaviour across all supported platforms and sink types while taking advantage of hardware volume control where available.

### Bit Depth Handling (Linux PulseAudio)

| Sink Format  | MA Delivery                       | PA Stream Format  | Conversion                         |
|--------------|-----------------------------------|-------------------|------------------------------------|
| `s16le`      | 16-bit PCM                        | `PA_SAMPLE_S16LE` | None                               |
| `s24le`      | 32-bit container (left-justified) | `PA_SAMPLE_S24LE` | Unpack int32, repack to 3-byte LE  |
| `s24-32le`   | 32-bit container                  | `PA_SAMPLE_S32LE` | None                               |
| `s32le`      | 32-bit PCM                        | `PA_SAMPLE_S32LE` | None                               |

The ALSA direct backend always uses 16-bit int PCM (PortAudio `int16` dtype) regardless of hardware capability.

### File Structure

| File/Folder | Description |
|-------------|-------------|
| `__init__.py` | Provider entry point, config entries (backend selector on Linux), and setup |
| `provider.py` | `LocalAudioProvider` class |
| `sendspin_bridge.py` | Bridge manager and per-device bridge (PA on Linux PulseAudio, sounddevice on Linux ALSA and macOS); also owns remap-sink topology lifecycle |
| `pa_simple.py` | ctypes wrapper around `libpulse-simple`/`libpulse` for direct PCM output, PA sink/module hardware volume control, and module load/unload; PA sink enumeration via `pactl`; ALSA device enumeration via PortAudio; `suspend_resume_sink()` workaround in case a sound card stalls *(Linux only)* |
| `remap_topology.py` | Computes the per-zone and full-channel passthrough `module-remap-sink` topology for multi-channel cards *(Linux PulseAudio only)* |
| `constants.py` | Shared constants (UUID namespace, buffer sizes, backend selector values) and the `volume_pct_to_amplitude` audio taper used by both the hardware and software volume paths; taper range is configurable via `_TAPER_A` (default 40dB, suited for receiver/outdoor setups) |
| `manifest.json` | Provider metadata and dependencies |
| `strings.json` | Localized config entry labels/descriptions (audio backend selector and its options) |


## Dependencies

- **Sendspin provider** (`depends_on: sendspin`): Required for audio synchronization and player management
- **libpulse-simple** *(Linux PulseAudio backend)*: PulseAudio simple client library accessed via ctypes for direct PCM streaming to sinks
- **libpulse** *(Linux PulseAudio backend)*: Full PulseAudio client library accessed via ctypes for hardware sink volume control and remap-sink module load/unload (`libpulse.so.0` is a transitive dependency of `libpulse-simple.so.0`, so no additional package is required)
- **pactl** *(Linux PulseAudio backend)*: Used for PA sink enumeration via `--format=json`. Requires `pulseaudio-utils` to be installed
- **sounddevice** *(Linux ALSA backend and macOS)*: Python bindings for PortAudio, used for audio output and device enumeration
- **numpy**: Used for PCM volume scaling and 24-bit format conversion

## Multi-Channel Sound Cards: Self-Managed Remap-Sink Topology

Multi-channel sound cards (5.1, 7.1 surround) expose a single multi-channel PulseAudio sink by default. To use each channel pair as an independent MA player, the provider creates its own `module-remap-sink` topology on startup — no external addon or pre-configuration required.

For every ALSA-card master sink with more than 2 channels, the provider creates:

- One stereo **zone sink** per recognized channel pair present in the master's channel map — `<card>_front_stereo`, `<card>_rear_stereo`, `<card>_side_stereo`, `<card>_center_sub` (front-center + LFE). Each zone sink always exposes a standard stereo (front-left, front-right) interface to clients, regardless of which physical channels it maps to, and gets its own independent hardware volume control
- One full-channel **"multichannel stereo" passthrough sink** — `<card>_multichannel_stereo` — for cards with 6 or more channels. This is a 1:1 passthrough (same channel count and channel map as the master, `remix=no`) with its own independent hardware volume, named after the equivalent AVR "Multi Channel Stereo" / "All Channel Stereo" mode

The card's raw multi-channel master sink (e.g. a 7.1 ALSA sink) is *not* registered as its own player once its remap-sink topology exists — it's fully covered by the zone sinks plus the passthrough sink, which together provide independent per-zone volume control and a "play to all outputs" option.

This topology is:

- **Idempotent**: on every startup, existing remap sinks (matching the naming convention) are detected and left untouched; only missing sinks are created
- **Self-cleaning**: all remap sinks created by the provider are unloaded when the provider stops or reloads
- **Automatic**: no addon, manual `pactl` setup, or configuration file is needed — card names are normalized from `alsa.card_name` (e.g. "Creative X-Fi" → `Creative_X_Fi`) to build sink names

After creating the remap-sink topology for a master sink, the provider:

- **Pins the master sink volume to 100%** via `PAVolumeController` so it never attenuates remap sinks feeding through it. The master has no bridge of its own, so without this explicit pin `module-device-restore` or any other PA client could leave it at an arbitrary level, stacking a hidden attenuation on top of every zone sink's volume
- **Runs a suspend/resume cycle** on the ALSA master sink via `pactl suspend-sink`. This resets the ALSA driver state after remap-sink creation and works around driver mmap regressions (notably the `snd_ctxfi` regression in kernel 6.12.x, commit `391e69143d0a`) that cause `pa_simple_write` to timeout silently on first use. The cycle is safe to run on any ALSA-card master and has no effect on cards that don't have the issue

## Notes

- On Linux, multi-channel sinks (5.1, 7.1) are supported on the PulseAudio backend via the self-managed remap-sink topology described above — the raw multi-channel master is not registered as a separate player once its zone sinks and passthrough sink exist.
- Virtual sinks created by `module-remap-sink` (zone sinks and the multichannel-stereo passthrough) are fully supported on the PulseAudio backend and are the recommended way to expose individual speaker pairs as independent MA players.
- On Linux, `pactl --format=json` is used for PA sink enumeration because it always reports the sink's native sample rate and format, unlike libpulse which reports the currently negotiated stream format when streams are active.
- PA sink enumeration requires `pactl` from `pulseaudio-utils` to be installed on the host.
- Sample rate and bit depth on the PulseAudio backend are determined by the PA daemon configuration (`/etc/pulse/daemon.conf`) and the sink's native hardware capabilities — they are not configurable per-player in MA.
- On the ALSA direct backend, PortAudio enumerates only real hardware `hw:` nodes. Virtual PCM plugins (`sysdefault`, `front`, `dmix`, `surround*`, etc.) are excluded. If a device cannot be opened exclusively (e.g. another process holds it), it is silently skipped during enumeration.
- On the ALSA direct backend, hardware ALSA mixer levels are not managed by MA. Set all relevant controls (Master, PCM) to 100% using `alsamixer -c <card>` and persist them with `sudo alsactl store <card>`.
- Plugging or unplugging a USB audio device on Linux triggers a full provider reload (an upstream MA behavior, not specific to this provider). All active playback on all local audio players stops abruptly during the reload, which typically completes within a second; the new/removed device is reflected automatically and players resume normally on the next playback request. The remap-sink topology is recreated idempotently as part of this reload.
- If a player provider reload is needed (e.g. after adding or removing PA sinks or ALSA devices), use **Settings → Providers → Local Audio Out → Reload** in the MA UI.
- PA sinks remain in **RUNNING** state while the provider is active — pre-warmed streams are held open to enable warm stream handoff on play. Sinks return to IDLE when the provider is disabled or MA is stopped. This is intentional: the warm streams are what eliminates cold-start sync offset in sync groups.
- On ALSA-card master sinks, the provider automatically runs a suspend/resume cycle at topology creation time to reset the ALSA driver state. This prevents silent output caused by driver mmap stalls (notably the `snd_ctxfi`) that can occur after a PA daemon restart or `daemon.conf` rate change. If you experience silent output from an ALSA card after a PA daemon restart without reloading the provider, manually run `pactl suspend-sink <master_sink_name> true && sleep 1 && pactl suspend-sink <master_sink_name> false` as a temporary workaround until the next provider reload.

## Related Documentation

- [Sendspin Provider](../sendspin/README.md)
