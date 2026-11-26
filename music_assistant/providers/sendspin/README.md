# Sendspin Player Provider

The Sendspin provider implements the [Sendspin Audio Protocol](https://github.com/Sendspin/spec), developed by the Open Home Foundation. It is the native playback protocol built into Music Assistant, providing synchronized audio playback across multiple clients.

## Overview

Sendspin enables:
- **Synchronized multi-room audio** with sample-accurate playback across devices
- **Per-player DSP processing** for individual equalizer and volume settings
- **Real-time metadata** including artwork, track info, and playback state
- **Bidirectional control** allowing clients to control playback

## Line-in sources

Besides its players, this provider delivers the inputs of Sendspin clients that
implement the [source role](https://github.com/Sendspin-Protocol/spec) (line-in,
turntable preamp, microphone, Bluetooth receiver) as Music Assistant audio
sources, playable on any player or group. `SendspinSources` in `sources.py`
implements them on the provider's own client connections.

### How it works

Every connected Sendspin client whose negotiated roles include the `source`
family shows up as one AudioSource under Live Inputs, unless the user disabled
its player or switched its Sendspin output off. The source role only
activates on paired connections, so an unpaired device never appears here.

When a user plays a source, the provider sends the client a
`server/command: start`. The client announces its native stream format,
streams timestamped encoded audio up to the server, and the Sendspin server
library decodes it back to PCM. The provider feeds that PCM into a clock
bridge (`aiosendspin.audio.AsrcSourceBridge`) and serves Music Assistant a
steady stream in the source's own format.

### Design notes

- **Output format.** A source's native format is only known once the
  client starts streaming, so the stream details wait for its first audio
  (falling back to 48 kHz / 24-bit / stereo when none arrives). The bridge
  keeps converting to that declared format when the source later restarts
  in another one, so the stream never changes format mid-play.
- **Clock bridge.** The client's capture clock (its ADC) and the consuming
  player's clock drift relative to each other, and capture timestamps can be
  gappy. The bridge holds a target latency of 500 ms and folds drift
  correction into a phase-continuous variable-rate resample (soxr).
- **Silence-hold.** A real line-in goes silent when unplugged, it does not
  stop. The stream mirrors that: when source audio stops flowing (stream
  end, disconnect, client unavailable) it keeps playing silence rather
  than ending. After the source timeout, the stream ends. This intentionally
  differs from providers like Spotify Connect, which end the stream immediately
  on pause: those have an upstream transport state to mirror, a line-in does not.
- **Reconnect recovery.** A reconnect clears the client's start request, so the
  provider re-sends it. The existing Music Assistant stream remains open with
  silence and resumes live audio after the client reconnects and rebuilds its
  latency buffer, provided audio returns before the source timeout. Audio
  captured during the disconnect is lost. Recovery is scoped to reconnects: a
  client that reports itself unavailable also has its start request cleared, but
  announces nothing when it returns, so that stream runs out the source timeout
  instead.
- **Server-initiated streaming only.** Per the Sendspin spec, a source client
  must not stream until the server asks. Streaming starts on source selection
  and stops on unselection, so no bandwidth is spent while nobody listens.

### Autostart

Source clients report whether a signal is present on their input, and older
clients only when they advertise the `line_sense` feature. Per the spec the
server decides what to do with that, and this provider turns it into
playback: pick a target under the device's own settings and the source
starts there when a signal appears, and stops again
after the signal has been gone for a minute. The target list covers everything
that renders audio, groups and multi-channel players included, and a device
that is a player itself defaults to playing its own line-in.

Only transitions act, and the first report after a connect is recorded
without acting, so a restart with the needle already down starts nothing. A
source that is already streaming is never re-targeted, so moving it by hand
survives the next transition. Autostop also closes a gap that exists without
line-sense: a client keeps streaming silence after a record ends, so the
no-audio timeout never fires and the queue would otherwise sit
playing silence indefinitely.

Older devices without `line_sense` have no trigger to offer, so they get no
setting and stay manual.

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

When 8927 is taken, the server uses the next free port and announces it over mDNS.

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
- **Player grouping** - Create multi-room audio groups. A group plays at the sample rate of what it plays, kept within the lowest and highest rate its members play; a member at another rate gets the group audio converted to its own rate
- **Metadata display** - Track info, artwork, progress
- **Playback control** - Play, pause, stop, next, previous
- **Repeat/Shuffle** - Queue control from clients

## Files

| File | Description |
|------|-------------|
| `provider.py` | Main provider class, handles WebRTC signaling and server lifecycle |
| `player.py` | Player implementation with playback, grouping, and metadata handling |
| `playback.py` | Playback pipeline with DSP channel processing and timed frame commits |
| `sources.py` | Line-in sources of source-role clients, with their clock bridge and autostart |
| `multi_channel.py` | Multi-channel players combined from Sendspin speakers |
| `__init__.py` | Provider setup and configuration |
| `manifest.json` | Provider metadata |

## Dependencies

- `aiosendspin` - Async Sendspin protocol implementation
- `aiolibdatachannel` - WebRTC implementation for Python (used for WebRTC bridging)
- `PIL/Pillow` - Image processing for artwork

## Players

Every Sendspin client is a protocol (`SendspinBasePlayer`) whose id is its client id, except a
speaker of a [multi-channel player](#multi-channel-players).

- A web or app player, a virtual player, a display, a visualizer, a light, a controller and an audio source exist only while they are connected (`session_only`): each is the native protocol of a player of its own, whose player id is its client id. Nothing is stored for one until its config is changed.
- Any other client is the native protocol of its player, unless that player has a protocol of its own from another provider: then it is an audio output of it. A client with a hardware MAC address that is seen for the first time joins the player of its device when there is one, as an audio output; it is the native protocol of a player of its own from its next connection on. A new client without one is the native protocol of a player of its own at once, as nothing else can tie it to a device.
- A client reports the MAC address it uses as client id or reports in its hello. A client whose id is not its MAC address also reports its id as an instance id, so several clients on one host stay separate players; a device whose client id changes from its MAC address to its key id keeps its player and settings.

## External Players (Protocol Bridges)

The Sendspin server supports external player registration for bridging other audio protocols. This enables cross-protocol grouping where Sendspin handles timing and synchronization.

### How External Players Work

External players are registered programmatically via `server_api.register_external_player()`:

1. The bridge provider creates a `ClientHelloPayload` with the device info and supported capabilities
2. The Sendspin server creates a `SendspinClient` and triggers `ClientAddedEvent`
3. The Sendspin provider creates a `SendspinPlayer` for this client, with the id of the bridged protocol as its underlying protocol
4. That Sendspin protocol is a derived audio protocol: it attaches to the player of the protocol it rides on, also while that protocol is away (through the player its record names), and is an available output only while that protocol is attached

A client whose record says it rides on another protocol is that bridge again when it connects before its bridge is set up, as the Sendspin Cast receiver app does after a restart: it is no browser player of its own then, although it reports as one.

The bridge managers (`SendspinBridgeManagerBase` in `bridge_manager.py`, subclassed by the AirPlay, squeezelite and Chromecast bridges) keep a bridge for the protocol that carries the Sendspin output of its player, for as long as that protocol is attached and the output is switched on. The interface they offer the bridged providers takes the protocol id of the bridged protocol.

A player has one Sendspin output, however many of its protocols can be bridged: one stored record with one switch and one set of settings. The record names its carrier, the protocol whose bridge registers the output, and keeps its client id for good. A bridge registers under the id of the record of its player; only a player without one gets a record, under an id of the device of the bridge that creates it. The player is what ties the bridges of one device together, because its protocols do not all report the same MAC address, or the same form of it.

A device has one output also while its protocols sit on two players: a bridge gets no output of its own while another player has the client id it would register under, in either form of the MAC address of the device, as a stored output or as the client of its bridge. It takes the id once that player lets go of it, such as when that player is removed for good. A player removed while none of its protocols is loaded (its device away since the start) wakes nobody: the other player takes the id at its next evaluation, such as when one of its protocols reconnects. Two players that each have an output of the device stored keep both; whichever bridge registers first has the device while it holds the client, and the other waits for it. Unlinking the protocol that carries the output from its player moves the record along, as every protocol riding on it moves.

Which protocol carries the output is decided in one place (`select_carrier`): the first in `BRIDGE_ORDER` (squeezelite, AirPlay, Chromecast) that holds it. A protocol the user has switched on holds the output when its manager says it qualifies while it is attached, and when it is the stored carrier while it is away or its provider still loads. So a speaker that is merely off keeps its place, while a protocol the user switched off, or whose provider is disabled, lets the next one take the output over. A protocol that never carried the output holds no place while it is away, because whether it qualifies is only known with its device there.

A hand-over never removes or creates the record. Under a lock per player, one step without a suspension lets the record follow its carrier (the carrier it names, the static delay of that carrier and what Chromecast asks for, below); then the bridge of the carrier before stops and the bridge of the new one registers under the same client id. The static delay is kept per carrier: the delay set for the carrier before is put aside in the record (`sendspin_bridge_static_delays`, by protocol id), and the one set while the new carrier had the output before comes back; without one, the default of the new bridge applies.

Sendspin over Chromecast is experimental. Chromecast leaves an output it carries switched off until the user switched it on once while Chromecast carried it (`sendspin_cast_opt_in`), and stores its warning with it (`protocol_experimental_note`). What it switched off is marked as its default (`protocol_off_by_default`), which tells that apart from an output its user switched off: AirPlay and squeezelite switch a default on when they take the output over, and leave an output its user switched off as it is. A save that switches an output drops the mark. An unchanged output switch submitted with a settings form leaves the carrier's default alone.

The bridges of a player are evaluated when one of its bridged protocols attaches or detaches, when its outputs change, when a provider loads or unloads, when its Sendspin output is switched and when a config of the player is saved.

### Implemented Bridges

| Provider | Bridge Location | Identifier |
|----------|-----------------|------------|
| AirPlay | `airplay/sendspin_bridge.py` | MAC address |
| Squeezelite | `squeezelite/sendspin_bridge.py` | MAC address |
| Chromecast | `chromecast/sendspin_bridge.py` | MAC address (Cast group: its uuid) |

### Implementing a New Bridge

To bridge another protocol to Sendspin:

1. Create a `ClientHelloPayload` with the device's capabilities
2. Call `register_external_player()` with an `on_stream_start` callback
3. Create a custom `Role` subclass to receive audio via `on_audio_chunk()`
4. Register the id of the protocol it rides on with `register_bridge_underlying_protocol()` before the client is registered

See the [AirPlay Sendspin Bridge](../airplay/sendspin_bridge.py) for a complete implementation example.

## Virtual Players

Other providers (typically plugins) can host a shared listening session on a
hidden, server-side "anchor" player via the public virtual player API:

```python
sendspin = mass.get_provider("sendspin")
player_id = await sendspin.create_virtual_player(
    owner_instance_id=self.instance_id,
    display_name="My Session",
)
...
await sendspin.remove_virtual_player(player_id)
```

A virtual player owns its own PlayerQueue and leads a native Sendspin group,
but never renders audio itself. Guest players (e.g. web players) are attached
and detached through standard grouping (`mass.players.cmd_set_members`) and
receive the audio stream, with join-catchup for late joiners. Because the
anchor is server-side and permanent for the session's lifetime, guests coming
and going never cause a group leader transfer.

Virtual players are hidden from the UI and not exposed to Home Assistant by
default, expose no volume controls (member volumes apply instead), and are
removed automatically when the owning provider unloads. Stale configurations
of virtual players whose owner no longer exists are swept on provider startup.

Virtual players are never auto-restored: when the Sendspin provider reloads
(or the server restarts), the owning provider must call `create_virtual_player`
again to restore its session anchor. A virtual player is a session-only player
with its client id as player id, so nothing is stored for it unless its config
is changed. A stored configuration (including the owner marker) is reused when
the same owner recreates the same player id; it is deleted on
`remove_virtual_player` or swept at startup once the owner provider no longer
exists.

## Multi-channel players

A multi-channel player combines Sendspin speakers into one player, each speaker on a channel of
its own (`players/create_multi_channel_player`, offered through
`ProviderFeature.CREATE_MULTI_CHANNEL_PLAYER`). The layouts are a stereo pair, a stereo pair with
a subwoofer (2.1) and one speaker on the center channel with a subwoofer (1.1); the speakers other
than the subwoofer are its main speakers. `players/multi_channel_layouts` lists the layouts with
their channels in order.

- The player is a silent server-side client, like a virtual player, with a stored protocol
  `sendspin_mc_<group id>`. Its record holds the group id (32 random bytes in base64url, as the
  Sendspin spec defines it) and per speaker its client id, name, channel and output delay
  (`speaker_<n>_*`). Combining removes the own Sendspin protocol of each speaker; a player that
  has no other protocol left goes with it.
- Only the player writes its record. Its group id and the client id, name and channel of each
  speaker have no config entries, so a config save keeps them as they are. Each speaker has
  these entries:
  - `speaker_<n>_state`: a label that says whether the speaker is online.
  - `speaker_<n>_display_name`: a label with the name of the speaker.
  - `speaker_<n>_channel_setting`: the channel the user sets, which the player takes into its
    record, swapping with the speaker that had that channel; a value that is no channel of the
    layout changes nothing, and neither do channels set in one save that make up no layout.
  - `speaker_<n>_output_delay`: the output delay of the speaker, which also follows a delay the
    speaker reports itself.
- A set with a subwoofer has the settings of its crossover, above its speakers: whether the
  subwoofer has a crossover of its own, whether the main speakers are high-passed, the crossover
  frequency and the level of the subwoofer. Each is stored only once the user changes it, so a
  set without them plays with the defaults. A change starts the stream again.
- A speaker of a set is never a protocol. The provider knows the speakers of every stored set
  before its server starts and hands a speaker that connects to its set, so it does not matter
  whether the set or its speakers come first. The set takes its speakers into the Sendspin group
  it plays in, also when it joins another leader. Group members and leaders leave the speakers
  out: a group lists the set.
- The session of the leader gives each speaker a pipeline of its own: the DSP of the set, then
  the channel of the speaker, while it is in the group of the set. The subwoofer and a center
  speaker play the mono sum. The streams controller adds the crossover: the subwoofer is
  low-passed at the crossover frequency, unless it has a crossover of its own, and plays at its
  level; a main speaker is high-passed there while the subwoofer plays, unless the user lets the
  main speakers play the full range. Both filters are Linkwitz-Riley filters of 24 dB/octave,
  which sum flat; next to main speakers at full range the subwoofer gets one of 12 dB/octave,
  which adds the least bass around the crossover. The level of the subwoofer goes no higher than
  0 dB, as more clips loud bass. The signal path of the player is that of its main speakers:
  it reports the mono sum of a center speaker, and a player that splits the bass is never shown
  as bit perfect.
- While a main speaker is away the other main speakers play every channel, and while the
  subwoofer is away the main speakers play the full range; such a change starts the stream
  again, as a DSP change does. A set is unavailable while none of its main speakers is
  connected, and the playback it leads stops then. The subwoofer is in the group only while a
  main speaker is, so it never plays on alone, also not for a player the set follows; it is
  back with the first main speaker. The silent client gets no pipeline and does not count for
  the sample rate of the session.
- A speaker is away while it is disconnected, and while it is out of the group of its set. One
  that leaves by itself, as it is unavailable or went to a group of its own, comes back at the
  next playback session, or when it moves back into the group by itself; its switch finds that
  group only while another speaker or player is connected in it. One that the group of another
  player takes (its switch) is taken back at once.
- One volume: a level set on the set goes to every speaker, and a level a speaker reports itself
  goes to the others. A speaker that reports back a level the set sent changes nothing.
- A speaker can be added to a set (`players/add_multi_channel_speaker`) and removed from it
  (`players/remove_multi_channel_speaker`, by its number), as long as the speakers make up a
  layout: a subwoofer goes into a stereo pair and out again. Adding takes the speaker as combining
  does; removing lets it go as taking apart does, and the speakers after it move up a number with
  all their `speaker_<n>_*` values, so the numbers stay without a gap.
- Taking a set apart is removing its player: its speakers register as clients of their own again,
  one that is away once it connects again. A speaker belongs to one set; a second request for it
  is refused.

## Related Documentation

- [Sendspin Protocol Specification](https://github.com/Sendspin/spec)
- [Music Assistant Remote Access](../../controllers/webserver/README.md)
