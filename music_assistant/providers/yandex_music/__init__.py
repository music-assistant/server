"""Yandex Music provider support for Music Assistant."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    CONF_ACTION_CLEAR_AUTH,
    CONF_BASE_URL,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_PRELOAD_BUFFER_MB,
    CONF_QUALITY,
    CONF_STREAM_BUFFER_MB,
    CONF_STREAMING_MODE,
    CONF_TOKEN,
    DEFAULT_BASE_URL,
    QUALITY_BALANCED,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_SUPERB,
    STREAMING_MODE_BUFFERED,
    STREAMING_MODE_DIRECT,
    STREAMING_MODE_PRELOAD,
)
from .provider import YandexMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


# Compatibility shim: advanced= was added in models >=1.1.87; stable uses 1.1.86
_ADVANCED: Any = (
    {"advanced": True}
    if any(f.name == "advanced" for f in dataclasses.fields(ConfigEntry))
    else {"category": "advanced"}
)

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    # Handle clear auth action
    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_TOKEN] = None

    # Check if user is authenticated
    is_authenticated = bool(values.get(CONF_TOKEN))

    return (
        # Authentication
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Yandex Music Token",
            description="Enter your Yandex Music OAuth token. "
            "See the documentation for how to obtain it.",
            required=True,
            hidden=is_authenticated,
            value=cast("str", values.get(CONF_TOKEN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Reset authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            hidden=not is_authenticated,
        ),
        # Quality
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            label="Audio quality",
            description="Select preferred audio quality.",
            options=[
                ConfigValueOption("Efficient (AAC ~64kbps)", QUALITY_EFFICIENT),
                ConfigValueOption("Balanced (AAC ~192kbps)", QUALITY_BALANCED),
                ConfigValueOption("High (MP3 ~320kbps)", QUALITY_HIGH),
                ConfigValueOption("Superb (FLAC Lossless)", QUALITY_SUPERB),
            ],
            default_value=QUALITY_BALANCED,
        ),
        # Streaming mode (advanced)
        ConfigEntry(
            key=CONF_STREAMING_MODE,
            type=ConfigEntryType.STRING,
            label="FLAC streaming mode",
            description="How encrypted FLAC streams are handled. "
            "'Direct' streams and decrypts on-the-fly (fast devices). "
            "'Buffered' decouples download from decryption via async queue (recommended). "
            "'Preload' downloads the full file before decryption (slow devices).",
            options=[
                ConfigValueOption("Direct (on-the-fly)", STREAMING_MODE_DIRECT),
                ConfigValueOption("Buffered (async queue, recommended)", STREAMING_MODE_BUFFERED),
                ConfigValueOption("Preload (full download first)", STREAMING_MODE_PRELOAD),
            ],
            default_value=STREAMING_MODE_BUFFERED,
            **_ADVANCED,
        ),
        ConfigEntry(
            key=CONF_PRELOAD_BUFFER_MB,
            type=ConfigEntryType.INTEGER,
            label="Preload max file size (MB)",
            description="Maximum file size (MB) for Preload mode. "
            "Files within this limit are fully downloaded and decrypted before playback, "
            "enabling seek and accurate progress. "
            "Files exceeding this limit automatically use Buffered mode (streaming without seek). "
            "Default: 100 MB (typical FLAC track is 30-50 MB).",
            range=(10, 500),
            default_value=100,
            **_ADVANCED,
            depends_on=CONF_STREAMING_MODE,
            depends_on_value=STREAMING_MODE_PRELOAD,
        ),
        ConfigEntry(
            key=CONF_STREAM_BUFFER_MB,
            type=ConfigEntryType.INTEGER,
            label="Stream buffer size (MB)",
            description="Memory buffer for Buffered streaming mode "
            "(also applies when Preload mode falls back to Buffered for oversized tracks). "
            "Larger values help with slow or unstable connections "
            "by allowing more audio to be downloaded ahead of playback. "
            "Default: 8 MB (~45 seconds of FLAC audio).",
            range=(1, 64),
            default_value=8,
            **_ADVANCED,
            depends_on=CONF_STREAMING_MODE,
            depends_on_value_not=STREAMING_MODE_DIRECT,
        ),
        # My Wave maximum tracks (advanced)
        ConfigEntry(
            key=CONF_MY_WAVE_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="My Wave maximum tracks",
            description="Maximum number of tracks to fetch for My Wave playlist. "
            "Lower values load faster but provide fewer tracks. Default: 150.",
            range=(10, 1000),
            default_value=150,
            required=False,
            **_ADVANCED,
        ),
        # Liked Tracks maximum tracks (advanced)
        ConfigEntry(
            key=CONF_LIKED_TRACKS_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Liked Tracks maximum tracks",
            description="Maximum number of tracks to show in Liked Tracks virtual playlist. "
            "Lower values load faster. Default: 500.",
            range=(50, 5000),
            default_value=500,
            required=False,
            **_ADVANCED,
        ),
        # API Base URL (advanced)
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            label="API Base URL",
            description="API endpoint base URL. "
            "Only change if Yandex Music changes their API endpoint. "
            "Default: https://api.music.yandex.net",
            default_value=DEFAULT_BASE_URL,
            required=False,
            **_ADVANCED,
        ),
    )
