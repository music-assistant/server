# Sendspin Source

Exposes Sendspin clients that implement the [source role](https://github.com/Sendspin-Protocol/spec)
(line-in, turntable preamp, microphone, Bluetooth receiver) as Music Assistant
audio sources, playable on any player or group.

## How it works

Every connected Sendspin client whose negotiated roles include the `source`
family shows up as one AudioSource under Live Inputs. The source role only
activates on paired connections, so an unpaired device never appears here.

When a user plays a source, the provider sends the client a
`server/command: start`. The client announces its native stream format,
streams timestamped encoded audio up to the server, and the Sendspin server
library decodes it back to PCM. The provider feeds that PCM into a clock
bridge (`aiosendspin.audio.AsrcSourceBridge`) and serves Music Assistant a
steady 48 kHz / 16-bit / stereo stream.

## Design notes

- **Fixed output format.** A source's native format is only known once the
  client starts streaming, but Music Assistant needs the stream format before
  that. The bridge converts whatever the client sends to the fixed declared
  format, so format discovery never blocks stream setup.
- **Clock bridge.** The client's capture clock (its ADC) and the consuming
  player's clock drift relative to each other, and capture timestamps can be
  gappy. The bridge holds a configurable target latency (default 500 ms) and
  folds drift correction into a phase-continuous variable-rate resample
  (soxr). If soxr is unavailable it falls back to the simple bridge, which
  drops/inserts samples on persistent deviation instead.
- **Silence-hold.** A real line-in goes silent when unplugged, it does not
  stop. This provider mirrors that: when source audio stops flowing (stream
  end, disconnect, client unavailable) the stream keeps playing silence and
  recovers automatically if the source returns, including re-sending the
  start command after a reconnect. After 30 seconds without source audio the
  stream ends and the queue stops. This intentionally differs from providers
  like Spotify Connect, which end the stream immediately on pause: those have
  an upstream transport state to mirror, a line-in does not.
- **Server-initiated streaming only.** Per the Sendspin spec, a source client
  must not stream until the server asks. Streaming starts on source selection
  and stops on unselection, so no bandwidth is spent while nobody listens.

## Evaluating audio continuity

The clock bridge logs the corrections it applies at DEBUG under the
`aiosendspin.audio.bridge` logger. Enabling it surfaces discrete events
(silence inserted for capture gaps, dropped out-of-order or overflow audio,
underruns, buffer resets) and a periodic occupancy heartbeat that reports the
buffered latency against the target, plus the applied resample ratio and the
measured source rate estimate (both in ppm). A healthy source sits near the
target with the ratio settled at the estimate and no discrete events. The
estimate is the source clock's true skew; a real ADC reads tens to low
hundreds of ppm. A saturated ratio means the skew exceeds the bridge's
correction cap and the stream is degrading to drops or silence padding.

## Out of scope (for now)

- Line-sense (`signal` present/absent) is logged but not acted on.
  Auto-starting playback when the turntable needle drops is a possible
  follow-up.
- Per-source latency overrides; the target latency is a provider-level
  setting.
