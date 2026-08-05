"""
DLNA Receiver — Music Assistant Plugin Provider.

Exposes Music Assistant as a UPnP/DLNA MediaRenderer so that external
applications (Qobuz, BubbleUPnP, foobar2000, mconnect, etc.) can discover
and cast audio streams to any MA player.

Architecture
~~~~~~~~~~~~
1. SSDP advertisement  — announces virtual MediaRenderers on the LAN
2. UPnP HTTP server     — serves device/service XML descriptions and
                          accepts SOAP control actions (AVTransport,
                          RenderingControl, ConnectionManager)
3. AudioSource bridge   — received audio URL is fed into the MA streaming
                          pipeline as an AudioSource media item, routed to
                          the corresponding target player

Multi-player mode
~~~~~~~~~~~~~~~~~
When ``target_players`` contains multiple comma-separated player_id values
(or the special value ``*``), the provider creates one virtual DLNA
renderer per player, each with a unique UDN and HTTP port.  DLNA control
points see each renderer as a separate device — e.g.
"Music Assistant — Kitchen", "Music Assistant — Living Room".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Set up the DLNA Receiver provider."""
    # Deferred to avoid loading music_assistant internals at module import time.
    from .provider import DLNAReceiverProvider  # noqa: PLC0415, RUF100

    return DLNAReceiverProvider(mass, manifest, config)
