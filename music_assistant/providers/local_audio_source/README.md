# Local Audio Source — the AUX-in cable for Music Assistant

> Local Audio Source is Music Assistant’s **Virtual AUX Input**, letting you capture any audio from your PC (via PulseAudio/PipeWire) and stream it to any Music Assistant player.

---

## ✨ What it does

- **Bring outside audio into Music Assistant.** Use your computer as the bridge.
- **Any PulseAudio/PipeWire source.** Line-in jacks, USB audio interfaces, built-in mic/line sources, Bluetooth receivers, or even monitor sources (capture what's currently playing on a sink).
- **Simple to set up.** Pick the source from a dropdown, start the stream, and it shows up as a source in your players.
- **Custom source personalization.** Give it your own display name and thumbnail — pick a bundled icon or use your own image URL.

---

## 🧩 Use cases

- **Quick-Connect Bluetooth Receiver**
  Plug a Quick Connect BT receiver into your PC's line-in (or pair it directly as a Bluetooth audio source). Anyone can quickly pair their phone without having to confirm the connection, and their music instantly plays across your whole-house system.

- **Announcements & Paging Microphone**
  Plug in a USB microphone and use it for announcements. Great for paging in a business, office, or house intercom setup.

- **Vinyl Turntable/Player**
  Connect your turntable (via phono preamp) directly to your PC's line-in, and enjoy your vinyl collection throughout your Music Assistant ecosystem.

---

## ✅ Requirements

- **Music Assistant** server.
- **Linux host** with PulseAudio or PipeWire (with its PulseAudio-compatible layer, `pipewire-pulse`) running.
- `pulseaudio-utils` (provides `pactl`, used for device discovery) and `libpulse0` (provides `libpulse-simple.so.0`, used for the actual audio capture).
- A capture source (line-in, USB interface, mic, Bluetooth receiver, etc).

---

## ⚙️ Configuration

- **Display Name** – what shows up in source lists.
- **Thumbnail** – pick a bundled icon (Bluetooth, Cable, Vinyl, Stereo, Chromecast, …) or a custom image URL.
- **Audio Input Device** – dropdown of PulseAudio/PipeWire sources detected via `pactl list sources`, including monitor sources.

Sample rate and channel count are fixed at 44.1kHz stereo; PulseAudio/PipeWire transparently resample and remap whichever source you pick to that format, so this works regardless of the source's native rate/channel count (e.g. a mono USB mic).

---

## ▶️ Using it

1. Select **Local Audio Source** as the input on your player.
2. Start playback — it streams live from your chosen source.
3. Stop when done; the plugin cleans up automatically.

---

## 📦 Docker notes

If you're running Music Assistant in Docker, the container needs access to the host's PulseAudio/PipeWire socket rather than raw ALSA devices.

```yaml
services:
  music-assistant:
    image: ghcr.io/music-assistant/server:latest
    volumes:
      - /run/user/1000/pulse:/run/pulse   # forward the PulseAudio/PipeWire-pulse socket
    environment:
      - PULSE_SERVER=unix:/run/pulse/native
    group_add:
      - "audio"
```

Adjust the host-side socket path (`/run/user/<uid>/pulse`) to match your PulseAudio/PipeWire user session; for a system-wide PipeWire/Pulse instance it may instead live at `/run/pulse` directly.
