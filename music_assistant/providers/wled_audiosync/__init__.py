"""
WLED Audio Sync Plugin Provider for Music Assistant.

Bridges WLED Audio Sync receivers (devices running upstream WLED v16.0.0+
or the MoonModules fork with the AudioReactive usermod loaded) to MA via
Sendspin's VISUALIZER role. Each WLED appears in MA as a sync-group-only
visualizer player; when group members play audio, the bridge consumes the
Sendspin-computed visualizer frames, repacks them as 44-byte WLED V2
audio-sync UDP packets, and schedules each send at audible-now via
`compute_play_time()`.

See `README.md` for the design / protocol / clean-room declaration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    CONF_DUPLICATE_TRANSMIT,
    CONF_MANUAL_PLAYERS,
    CONF_MULTICAST_TTL,
    CONF_REQUIRE_AUDIOREACTIVE,
    DEFAULT_DUPLICATE_TRANSMIT,
    DEFAULT_REQUIRE_AUDIOREACTIVE,
)
from .provider import WledAudioSyncProvider
from .wled_audiosync_bridge import DEFAULT_MULTICAST_TTL

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the WLED Audio Sync plugin provider instance."""
    return WledAudioSyncProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries used to set this provider up."""
    return (
        ConfigEntry(
            key=CONF_MANUAL_PLAYERS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Manually-added WLED destinations",
            default_value=[],
            required=False,
            description=(
                "WLED targets that don't show up in mDNS - typically broadcast or "
                "multicast endpoints. One entry per line, formatted "
                "'<friendly name>=<address>'. Multicast addresses (e.g. 239.0.0.1) "
                "are detected automatically. Examples: "
                "'Living Room Multicast=239.0.0.1', 'LAN Broadcast=192.168.1.255'."
            ),
        ),
        ConfigEntry(
            key=CONF_REQUIRE_AUDIOREACTIVE,
            type=ConfigEntryType.BOOLEAN,
            label="Only register WLED devices that report the AudioReactive usermod",
            default_value=DEFAULT_REQUIRE_AUDIOREACTIVE,
            required=False,
            description=(
                "When enabled, each discovered WLED device is probed at "
                "http://<addr>/json/info and only those whose response includes "
                "the AudioReactive usermod are bridged. Disable to bridge "
                "every WLED discovered via mDNS (useful for testing "
                "vanilla-WLED devices which will simply ignore V2 packets)."
            ),
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_DUPLICATE_TRANSMIT,
            type=ConfigEntryType.BOOLEAN,
            label="Duplicate-transmit each frame",
            required=False,
            default_value=DEFAULT_DUPLICATE_TRANSMIT,
            description=(
                "Send each V2 audio-sync packet twice back-to-back. Mirrors "
                "the WLED firmware behaviour seen in real captures and "
                "improves resilience against single packet loss. Applies to "
                "every bridge under this provider."
            ),
        ),
        ConfigEntry(
            key=CONF_MULTICAST_TTL,
            type=ConfigEntryType.INTEGER,
            label="Multicast TTL",
            required=False,
            default_value=DEFAULT_MULTICAST_TTL,
            range=(1, 255),
            description=(
                "IP TTL for outgoing multicast packets. Only takes effect for "
                "bridges whose destination is a multicast group. Increase "
                "only if your multicast traffic must cross routers."
            ),
            advanced=True,
        ),
    )
