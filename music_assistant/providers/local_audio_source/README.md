# Local Audio Source — the AUX-in cable for Music Assistant

> Local Audio Source is Music Assistant’s **Virtual AUX Input**, letting you capture any audio from your PC (ALSA via arecord) and stream it to any Music Assistant player.

---

## ✨ What it does

- **Bring outside audio into Music Assistant.** Use your computer as the bridge.  
- **Any ALSA input.** Line-in jacks, USB audio interfaces, or built-in mic/line sources.  
- **Simple to set up.** Choose the input, start the stream, and it shows up as a source in your players.  
- **Custom source personalization.** Give it your own display name and thumbnail image for a polished, personalized feel in the Music Assistant UI.  

---

## 🧩 Use cases

- **Quick-Connect Bluetooth Receiver**  
  Plug a Quick Connect BT receiver into your PC’s line-in. Anyone can quickly pair their phone without having to confirm the connection, and their music instantly plays across your whole-house system.

- **Announcements & Paging Microphone**  
  Plug in a USB microphone and use it for announcements. Great for paging in a business, office, or house intercom setup.

- **Vynyl Turntable/Player**  
  Connect your turntable (via phono preamp) directly to your PC’s line-in, and enjoy your vinyl collection throughout your Music Assistant ecosystem.

---

## ✅ Requirements

- **Music Assistant** server.  
- **Linux host** with ALSA.  
- A capture device (line-in, USB interface, mic, etc).  

---

## ⚙️ Configuration

- **Display Name** – what shows up in source lists.  
- **Thumbnail Image (URL)** – optional icon.  
- **Audio Input Device** – detected ALSA devices or manual entry (`alsa:hw:X,Y`).  
- **Sample Rate** – 44.1kHz or 48kHz.  
- **Channels** – mono or stereo.  
- **Period/Buffer (µs)** – tuning for latency vs stability.  

> ℹ️ Run `arecord -l` to list devices.  

---

## ▶️ Using it

1. Select **Local Audio Source** as the input on your player.  
2. Start playback—it streams live from your chosen device.  
3. Stop when done; the plugin cleans up automatically.  

---

## 📦 Docker notes

If you’re running Music Assistant in Docker, you need to give the container access to your audio devices.  

### ALSA devices
```yaml
services:
  music-assistant:
    image: ghcr.io/music-assistant/server:latest
    devices:
      - /dev/snd:/dev/snd   # forward ALSA devices
    group_add:
      - "audio"
