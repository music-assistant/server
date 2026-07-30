# Discovery Controller Architecture

This core controller owns network discovery in Music Assistant:

- the shared `AsyncZeroconf` instance and interface selection used by discovery consumers
- mDNS/Zeroconf browsing for provider-declared `mdns_discovery` subscriptions
- SSDP/UPnP search cycles for provider-declared `upnp_discovery` subscriptions
- Music Assistant server advertisement on `_mass._tcp.local.`

## Responsibilities

- Build a single shared Zeroconf browser from all provider manifests.
- Periodically run SSDP discovery for active providers and fan out results through provider callbacks.
- Replay cached mDNS results when a provider loads so providers do not need to wait for the next announcement.
- Register and unregister the Music Assistant server's own Zeroconf service.

## Provider Integration

Providers opt into discovery in `manifest.json`:

- `mdns_discovery`: list of Zeroconf service types
- `upnp_discovery`: list of SSDP search targets

Providers then implement the matching callbacks in their provider class:

- `on_mdns_service_state_change(...)`
- `on_upnp_service_discovered(...)`

Player providers can still keep `discover_players()` for provider-specific discovery paths that are not covered by shared network discovery, such as manual IP configuration or controller-side refresh logic.
