# Sendspin Player Provider

The Sendspin provider implements the [Sendspin Audio Protocol](https://github.com/Sendspin/spec), developed by the Open Home Foundation. It is the native playback protocol built into Music Assistant, providing synchronized audio playback across multiple clients.

## Overview

Sendspin enables:
- **Synchronized multi-room audio** with sample-accurate playback across devices
- **Per-player DSP processing** for individual equalizer and volume settings
- **Real-time metadata** including artwork, track info, and playback state
- **Bidirectional control** allowing clients to control playback

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Music Assistant Server                          │
│                                                                       │
│  ┌─────────────────┐     ┌─────────────────┐     ┌───────────────┐  │
│  │ SendspinProvider│────▶│  SendspinServer │────▶│ Audio Streams │  │
│  │                 │     │   (port 8927)   │     │               │  │
│  └─────────────────┘     └────────┬────────┘     └───────────────┘  │
│                                   │                                  │
└───────────────────────────────────┼──────────────────────────────────┘
                                    │
                 ┌──────────────────┼──────────────────┐
                 │                  │                  │
                 ▼                  ▼                  ▼
          ┌───────────┐      ┌───────────┐      ┌───────────┐
          │  Browser  │      │ Mobile App│      │  Hardware │
          │ (WebRTC)  │      │ (WebRTC)  │      │(WebSocket)│
          └───────────┘      └───────────┘      └───────────┘
```

## Connection Methods

### 1. Direct WebSocket Connection (Local Network)

Hardware devices and local clients can connect directly to the Sendspin server:

```
ws://<ma-server-ip>:8927/sendspin
```

This is suitable for:
- Hardware players on the local network
- Native apps with direct network access
- Development and testing

### 2. WebRTC Connection (Remote/NAT Traversal)

For web browsers and mobile apps that need to work across networks (including when accessing Music Assistant remotely), we use WebRTC DataChannels. The signaling happens through the authenticated MA API WebSocket connection.

#### WebRTC Connection Flow

```
┌──────────────┐                    ┌─────────────────┐
│   Client     │                    │   MA Server     │
│  (Browser)   │                    │                 │
└──────┬───────┘                    └────────┬────────┘
       │                                     │
       │  1. sendspin/ice_servers            │
       │────────────────────────────────────▶│
       │                                     │
       │  ICE servers (STUN/TURN)            │
       │◀────────────────────────────────────│
       │                                     │
       │  2. Create RTCPeerConnection        │
       │     Create DataChannel              │
       │                                     │
       │  3. sendspin/connect {offer}        │
       │────────────────────────────────────▶│
       │                                     │ Create RTCPeerConnection
       │                                     │ Connect to local Sendspin
       │  {session_id, answer, ice}          │
       │◀────────────────────────────────────│
       │                                     │
       │  4. sendspin/ice {candidate}        │
       │────────────────────────────────────▶│
       │                                     │
       │  5. DataChannel opens               │
       │◀═══════════════════════════════════▶│
       │     Sendspin protocol messages      │
       │                                     │
```

#### API Commands for WebRTC

The Sendspin provider registers these API commands for WebRTC signaling:

| Command | Parameters | Description |
|---------|------------|-------------|
| `sendspin/ice_servers` | None | Get ICE server configurations (STUN/TURN). Returns HA Cloud TURN servers if available. |
| `sendspin/connect` | `offer: {sdp, type}` | Initiate WebRTC connection with SDP offer. Returns `{session_id, answer, ice_candidates}`. |
| `sendspin/ice` | `session_id, candidate` | Exchange ICE candidates for NAT traversal. |
| `sendspin/disconnect` | `session_id` | Clean up WebRTC session. |

### ICE Server Configuration

The provider automatically provides optimal ICE servers:

1. **Home Assistant Cloud TURN servers** (if HA Cloud is available with active subscription)
   - Provides reliable connections through firewalls and symmetric NAT
   - Requires HA 2025.12.0b6 or later

2. **Public STUN servers** (fallback)
   - `stun:stun.l.google.com:19302`
   - `stun:stun.cloudflare.com:3478`
   - `stun:stun.home-assistant.io:3478`

## Implementing a Sendspin Client

### Web Browser (TypeScript/JavaScript)

For web browsers, use the WebRTC approach with the MA API for signaling:

```typescript
// 1. Get ICE servers from the server
const iceServers = await api.sendCommand("sendspin/ice_servers");

// 2. Create RTCPeerConnection
const peerConnection = new RTCPeerConnection({ iceServers });

// 3. Create DataChannel
const dataChannel = peerConnection.createDataChannel("sendspin", {
  ordered: true,
});

// 4. Create and send offer
const offer = await peerConnection.createOffer();
await peerConnection.setLocalDescription(offer);

const response = await api.sendCommand("sendspin/connect", {
  offer: { sdp: offer.sdp, type: offer.type },
});

// 5. Set remote description (answer)
await peerConnection.setRemoteDescription(
  new RTCSessionDescription(response.answer)
);

// 6. Add ICE candidates from server
for (const candidate of response.ice_candidates) {
  await peerConnection.addIceCandidate(new RTCIceCandidate(candidate));
}

// 7. Handle local ICE candidates
peerConnection.onicecandidate = (event) => {
  if (event.candidate) {
    api.sendCommand("sendspin/ice", {
      session_id: response.session_id,
      candidate: {
        candidate: event.candidate.candidate,
        sdpMid: event.candidate.sdpMid,
        sdpMLineIndex: event.candidate.sdpMLineIndex,
      },
    });
  }
};

// 8. Use dataChannel for Sendspin protocol
dataChannel.onopen = () => {
  // DataChannel ready - use sendspin-js library
};
```

### Mobile Apps

Mobile apps can use the same WebRTC approach for reliable connectivity across networks. The connection is established through the authenticated MA API, so no additional authentication is needed for the Sendspin connection itself.

### Hardware Devices

Hardware devices on the local network can connect directly via WebSocket:

```python
import websockets

async with websockets.connect("ws://192.168.1.100:8927/sendspin") as ws:
    # Sendspin protocol communication
    pass
```

## Player Features

Sendspin players support:

- **Volume control** - Set volume level (0-100) and mute
- **Synchronized playback** - Sample-accurate sync across grouped players
- **Per-player DSP** - Individual equalizer settings per device
- **Player grouping** - Create multi-room audio groups
- **Metadata display** - Track info, artwork, progress
- **Playback control** - Play, pause, stop, next, previous
- **Repeat/Shuffle** - Queue control from clients

## Files

| File | Description |
|------|-------------|
| `provider.py` | Main provider class, handles WebRTC signaling and server lifecycle |
| `player.py` | Player implementation with playback, grouping, and metadata handling |
| `timed_client_stream.py` | Multi-client audio stream distribution with timing |
| `__init__.py` | Provider setup and configuration |
| `manifest.json` | Provider metadata |

## Dependencies

- `aiosendspin` - Async Sendspin protocol implementation
- `aiortc` - WebRTC implementation for Python (used for WebRTC bridging)
- `PIL/Pillow` - Image processing for artwork

## Related Documentation

- [Sendspin Protocol Specification](https://github.com/Sendspin/spec)
- [Music Assistant Remote Access](../../controllers/webserver/README.md)
