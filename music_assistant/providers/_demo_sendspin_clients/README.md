# Demo Sendspin Clients

Fake Sendspin devices for exercising the pairing and approval screens without hardware.

Sendspin pairing is driven by what a *client* advertises in its hello: which pairing methods it
offers, whether it admits unpaired access, how a PIN reaches the operator, and where a static
secret is found. A Music Assistant player object carries none of that, so this provider connects
real `aiosendspin` clients to this server's own Sendspin endpoint, one per scenario.

Only loaded in dev mode, like every other `_`-prefixed provider.

## Using it

Enable the provider, pick the scenarios to run, and each one connects a device that shows up as
an ordinary Sendspin player needing setup. Run the setup flow on that player to see the screens.

The provider's own settings page is the device's front panel:

- the derived dynamic PIN, once the server asks for one
- the static PIN and the pairing token, so they can be copied into setup
- whether the device is waiting for its pairing button, and the button itself
- the reason the last pairing attempt was aborted

Nothing pushes to that page, so press **Refresh this device's status** after starting a pairing
attempt. Keep it open in a second tab next to the player's setup flow.

**Reset** makes a device forget the server and reconnect, and drops this server's pairing and
unpaired-access records for it, so a scenario can be run again from scratch.

## Gesture gating

A static-PIN pairing always waits for the device's pairing button. A dynamic PIN waits only when
the negotiated length is under six digits, or after repeated PIN failures. The negotiated length
is `max(device minimum, server minimum)`, so a device asking for four digits only gets four when
the Sendspin provider's own minimum is four as well.

Once a device is paired, the server opens the pairing window itself over a management session, so
the button is only needed for a first pairing.

## Scenarios

| Scenario | What it shows |
| --- | --- |
| Open Speaker | Guest access only: a single consent step, no pairing offered |
| Guest Speaker | Guest access with pairing offered as the optional secure alternative |
| PIN Speaker | Six-digit dynamic PIN on a display |
| Spoken PIN Speaker | Dynamic PIN spoken instead of displayed |
| Long PIN Speaker | Eight-digit PIN, rendered as two groups of four |
| Short PIN Speaker | Four-digit PIN, which is gesture-gated |
| Static PIN Speaker | Fixed eight-digit PIN, always gesture-gated |
| Dual PIN Speaker | Both PIN methods, so setup first asks which to use |
| Token Speaker | Pairing token found on the device |
| Managed Speaker | Pairing token handed out by an administrator |
| Locked Speaker | Nothing on offer, so setup can only abort |
| Everything Speaker | Guest access plus every method, on both PIN out-channels |
| Line-In Speaker | Adds an audio input, and with it the line-in decision step |

Audio is decoded and dropped, so the players are usable playback targets too.
