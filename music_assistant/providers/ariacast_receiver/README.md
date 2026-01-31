# AriaCast Receiver for Music Assistant

Stream audio wirelessly from your Android device to Music Assistant players. Think of it as **AirPlay for Music Assistant** - cast audio from any android phone to any speaker.

## Features

- 🎵 **High-Quality Audio**: 48kHz 16-bit stereo PCM streaming
- 📱 **Easy Discovery**: Automatic server detection via UDP broadcast
- 🎼 **Full Metadata**: Track title, artist, album, and artwork support
- 🔀 **Flexible Routing**: Stream to any Music Assistant player or group
- ⚡ **Low Latency**: 20ms frame duration with intelligent buffering


### Configuration

1. Enable **AriaCast Receiver** in Music Assistant settings by adding it as a provider
2. Configure basic settings:
   - **Server Name**: How it appears in discovery (default: "AriaCast Speaker")
   - **Target Player**: Auto or specific player
   - **Ports**: 12888 (discovery), 12889 (streaming)

### Usage

1. Install the AriaCast Android app
2. Open app and tap "Discover Servers"
3. Select your Music Assistant server
4. Play audio from any app - it streams to Music Assistant!

## Configuration Options

| Setting | Default | Description |
|---------|---------|-------------|
| Server Name | AudioCast Speaker | Name shown in client discovery |
| Connected Player | Auto | Target Music Assistant player |
| Streaming Port | 12889 | WebSocket audio port |
| Discovery Port | 12888 | UDP discovery port |
| Allow Player Switching | Yes | Enable manual source selection |


## Technical Details

### Audio Format
- Sample Rate: 48000 Hz
- Channels: 2 (Stereo)
- Bit Depth: 16-bit signed LE
- Frame Size: 3840 bytes (20ms)

### Protocol Endpoints
- **Discovery**: UDP port 12888 (`DISCOVER_AUDIOCAST`)
- **Audio Stream**: `ws://server:12889/audio`
- **Metadata Stream**: `ws://server:12889/metadata`
- **Metadata API**: `POST http://server:12889/metadata`

### Metadata Support
```json
{
  "title": "Song Name",
  "artist": "Artist Name",
  "album": "Album Name",
  "artwork_url": "https://...",
  "duration_ms": 240000,
  "position_ms": 30000
}
```

## Troubleshooting

### Server not found in app
1. Ensure both devices are on same network
2. Check firewall allows UDP port 12888
3. Verify Music Assistant is running
4. Try manual connection with IP address

### No audio playback
1. Check a Music Assistant player is available
2. Verify player is not already in use
3. Check Music Assistant logs for errors
4. Try switching to a different player

### Metadata not showing
1. Ensure client sends metadata in correct format
2. Check Music Assistant logs: `grep -i metadata`
3. Verify artwork URL is accessible

## Documentation

- **METADATA_GUIDE.md**: Complete metadata API documentation
- **ANDROID_TEST_INSTRUCTIONS.md**: Testing guide for Android app

## Architecture

```
Android App → UDP Discovery (12888)
           ↓
AriaCast Receiver Provider
           ↓
Music Assistant Player Controller
           ↓
Target Player (Sonos/Chromecast/etc.)
```

## Contributing

Report issues with:
- Music Assistant version
- Provider version
- Client app version
- Debug logs

## Credits

Built for Music Assistant by the community. Born out of the need for better casting integration.



