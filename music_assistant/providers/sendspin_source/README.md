# Sendspin Source

Exposes Sendspin clients that implement the [source role](https://github.com/Sendspin-Protocol/spec)
(line-in, turntable preamp, microphone, Bluetooth receiver) as Music Assistant
audio sources, playable on any player or group.

## Why a separate provider?

Music Assistant assigns each provider one type. The main Sendspin provider is a
player provider, so it cannot also expose captured inputs through the plugin
AudioSource interface. This separate plugin provides that interface while
reusing the main provider's client connections.

## How it works

Every connected Sendspin client whose negotiated roles include the `source`
family shows up as one AudioSource under Live Inputs. The source role only
activates on paired connections, so an unpaired device never appears here.

When a user plays a source, the provider sends the client a
`server/command: start`. The client announces its native stream format,
streams timestamped encoded audio up to the server, and the Sendspin server
library decodes it back to PCM. This provider feeds that PCM into a clock
bridge (`aiosendspin.audio.AsrcSourceBridge`) and serves Music Assistant a
steady 48 kHz / 16-bit / stereo stream.

## Design notes

- **Fixed output format.** A source's native format is only known once the
  client starts streaming, but Music Assistant needs the stream format before
  that. The bridge converts whatever the client sends to the fixed declared
  format, so format discovery never blocks stream setup.
- **Clock bridge.** The client's capture clock (its ADC) and the consuming
  player's clock drift relative to each other, and capture timestamps can be
  gappy. The bridge holds a configurable target latency and folds drift
  correction into a phase-continuous variable-rate resample (soxr).
- **Silence-hold.** A real line-in goes silent when unplugged, it does not
  stop. This provider mirrors that: when source audio stops flowing (stream
  end, disconnect, client unavailable) the stream keeps playing silence rather
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

## Autostart

Clients that advertise the `line_sense` feature report whether a signal is
present on their input. Per the spec the server decides what to do with that,
and this provider turns it into playback: pick a target under the device's own
settings and the source starts there when a signal appears, and stops again
after the signal has been gone for a minute. The target list covers everything
that renders audio, groups and stereo pairs included, and a device that is a
player itself defaults to playing its own line-in.

Only transitions act, and the first report after a connect is recorded
without acting, so a restart with the needle already down starts nothing. A
source that is already streaming is never re-targeted, so moving it by hand
survives the next transition. Autostop also closes a gap that exists without
line-sense: a client keeps streaming silence after a record ends, so the
30-second no-audio timeout never fires and the queue would otherwise sit
playing silence indefinitely.

Devices without `line_sense` have no trigger to offer, so they get no setting
and stay manual.

## Out of scope (for now)

- Per-source latency overrides; the target latency is a provider-level
  setting.
