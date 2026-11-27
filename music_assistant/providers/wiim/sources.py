"""
Source resolution for the WiiM provider.

The WiiM reports two different things about what it is playing: an *input mode* (Network,
Bluetooth, Optical In, TV, Phono In, Line In) and, while it streams from the network, the media
URL of whoever is streaming. Music Assistant plays through the device itself, so both have to be
translated into an MA source: unplugged inputs must show up as their own source, and a stream that
was started by another app (Tidal Connect, Spotify Connect, AirPlay, a DLNA push) must never be
reported as Music Assistant's own queue.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .constants import (
    DEVICE_INPUT_SOURCE_IDS,
    INPUT_MODE_SOURCES,
    SOURCE_AIRPLAY,
    SOURCE_EXTERNAL,
    SOURCE_NETWORK,
    SOURCE_SPOTIFY,
    SOURCE_UNKNOWN,
)

# Raw `mode` values from the device's HTTP API (getPlayerStatus / getStatusEx). The SDK only
# tracks the UPnP `playback_mode` string, which stays stale (or is missing) while a physical
# input is selected but silent, so the raw mode is the reliable signal for an input.
MODE_TO_SOURCE_ID: dict[str, str] = {
    "1": SOURCE_AIRPLAY,
    "2": "dlna",
    "3": SOURCE_NETWORK,
    "4": "line_in",
    "5": "bluetooth",
    "6": "optical",
    "10": SOURCE_NETWORK,
    "11": "usb",
    "20": SOURCE_NETWORK,
    "31": SOURCE_SPOTIFY,
    "34": "lyrion",
    "36": "qobuz",
    "40": "line_in",
    "41": "bluetooth",
    "43": "optical",
    "44": "aux_in",
    "47": "line_in",
    "49": "hdmi",
    "51": "usb",
    "54": "phono_in",
    "60": "line_in",
}

# Vendor reported by the device next to a network stream, mapped to the service source.
# (e.g. a Tidal Connect session reports mode "10" with vendor "Tidal".)
VENDOR_TO_SOURCE_ID: dict[str, str] = {
    "tidal": "tidal",
    "qobuz": "qobuz",
    "amazon": "amazon",
    "deezer": "deezer",
    "spotify": "spotify",
    "airplay": SOURCE_AIRPLAY,
    "youtube": "youtube",
    "soundcloud": "soundcloud",
}

# Fallback when the device reports no vendor: match the stream URL against the service hosts.
HOST_FRAGMENTS_TO_SOURCE_ID: tuple[tuple[str, str], ...] = (
    ("resources.tidal.com", "tidal"),
    ("tidal.com", "tidal"),
    ("qobuz.com", "qobuz"),
    ("amazon.com", "amazon"),
    ("deezer.com", "deezer"),
    ("scdn.co", "spotify"),
    ("spotify.com", "spotify"),
    ("youtube.com", "youtube"),
    ("soundcloud.com", "soundcloud"),
)

# Hosts of the Music Assistant server itself; a stream URL on one of these is our own playback.
DEFAULT_OWN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def input_source_for_mode(mode: str | None) -> str | None:
    """
    Return the input source id for a raw device mode.

    :param mode: The raw ``mode`` value reported by the device's HTTP API.
    """
    if not mode:
        return None
    source_id = MODE_TO_SOURCE_ID.get(str(mode).strip())
    return source_id if source_id in DEVICE_INPUT_SOURCE_IDS else None


def input_source_for_play_mode(play_mode: str | None) -> str | None:
    """
    Return the input source id for an SDK play mode (an ``InputMode`` display name).

    :param play_mode: The play mode as reported by the SDK (e.g. ``"Optical In"``).
    """
    if not play_mode or play_mode == SOURCE_NETWORK:
        return None
    play_mode_source = INPUT_MODE_SOURCES.get(play_mode)
    return play_mode_source.id if play_mode_source else None


def is_own_stream(uri: str | None, own_hosts: frozenset[str] | set[str]) -> bool:
    """
    Return whether a stream URL points at the Music Assistant server itself.

    :param uri: The media URL the device is playing.
    :param own_hosts: Host names/IPs that resolve to the Music Assistant server.
    """
    if not isinstance(uri, str) or not uri:
        return False
    parsed = urlparse(uri)
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "").lower() in own_hosts


def external_source_for(vendor: str | None, uri: str | None) -> str | None:
    """
    Return the streaming service source id for a stream started outside Music Assistant.

    :param vendor: The ``vendor`` value reported by the device's HTTP API, if any.
    :param uri: The media URL the device is playing, if any.
    """
    if isinstance(vendor, str) and vendor:
        source_id = VENDOR_TO_SOURCE_ID.get(vendor.strip().lower())
        if source_id:
            return source_id
    if isinstance(uri, str) and uri:
        lowered = uri.lower()
        for fragment, source_id in HOST_FRAGMENTS_TO_SOURCE_ID:
            if fragment in lowered:
                return source_id
    return None


def resolve_active_source(
    *,
    play_mode: str | None,
    mode: str | None,
    vendor: str | None,
    uri: str | None,
    own_hosts: frozenset[str] | set[str],
    own_stream_uri: bool = False,
    queue_id: str | None = None,
    queue_has_item: bool = False,
) -> str | None:
    """
    Resolve the Music Assistant source that is currently active on the device.

    A physical input always wins: it is audible regardless of what the device last streamed, and
    the SDK keeps reporting the previous network mode until something is played again.

    :param play_mode: The SDK play mode (an ``InputMode`` display name).
    :param mode: The raw ``mode`` value reported by the device's HTTP API.
    :param vendor: The ``vendor`` value reported by the device's HTTP API.
    :param uri: The media URL currently loaded on the device.
    :param own_hosts: Host names/IPs that resolve to the Music Assistant server.
    :param own_stream_uri: Whether the loaded URI is the stream Music Assistant started itself.
    :param queue_id: The MA queue id of this player, if it has one.
    :param queue_has_item: Whether that queue currently holds a playing item.
    """
    if (input_id := input_source_for_mode(mode)) or (
        input_id := input_source_for_play_mode(play_mode)
    ):
        return input_id

    device_uri = uri or ""
    if device_uri == "wiimu_airplay":
        return SOURCE_AIRPLAY
    if device_uri.startswith("spotify:"):
        return SOURCE_SPOTIFY
    # Our own stream beats the vendor: Music Assistant playing Tidal also reports vendor Tidal.
    if device_uri and (own_stream_uri or is_own_stream(device_uri, own_hosts)):
        return queue_id
    # A session started in another app usually has no URI at all on the device, only a vendor.
    if external_source := external_source_for(vendor, device_uri):
        return external_source
    if device_uri:
        return SOURCE_EXTERNAL
    # Nothing loaded: an MA queue item is only ours while the stream is being set up.
    return queue_id if (queue_has_item and queue_id) else SOURCE_UNKNOWN
