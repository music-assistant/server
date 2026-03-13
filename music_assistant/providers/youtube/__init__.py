"""YouTube provider for Music Assistant using yt-dlp.

Allows searching and playing YouTube videos as audio without requiring
a YouTube Music account or subscription. Uses yt-dlp for both search
and stream extraction.

Optional YouTube Data API v3 support improves search and metadata quality.
Optional cookie support allows access to age-restricted or member-only content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    CONF_API_KEY,
    CONF_COOKIES,
    CONF_PLAYLIST_LIMIT,
    DEFAULT_PLAYLIST_LIMIT,
    SUPPORTED_FEATURES,
)
from .provider import YouTubeProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YouTubeProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_API_KEY,
            type=ConfigEntryType.SECURE_STRING,
            label="YouTube Data API Key (optional)",
            description=(
                "Optional. Enter a YouTube Data API v3 key to use the official API "
                "for search and metadata. This improves reliability and avoids "
                "scraping limits. Create a key at "
                "https://console.cloud.google.com/apis/credentials"
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_PLAYLIST_LIMIT,
            type=ConfigEntryType.INTEGER,
            label="Artist playlist limit",
            description=("Maximum number of channel playlists to return as albums per artist."),
            required=False,
            default_value=DEFAULT_PLAYLIST_LIMIT,
            value=DEFAULT_PLAYLIST_LIMIT,
            range=(1, 100),
        ),
        ConfigEntry(
            key=CONF_COOKIES,
            type=ConfigEntryType.SECURE_STRING,
            label="YouTube cookie (for restricted videos)",
            description=(
                "Optional. Paste your YouTube cookies to enable playback of "
                "age-restricted or member-only content.\n\n"
                "How to obtain cookies:\n"
                "1. Install a browser extension such as 'Get cookies.txt LOCALLY' "
                "(Chrome/Edge) or 'cookies.txt' (Firefox).\n"
                "2. Log in to youtube.com with your Google account.\n"
                "3. Use the extension to export cookies for youtube.com "
                "in Netscape/cookies.txt format.\n"
                "4. Paste the full exported text here.\n\n"
                "Alternatively, open browser DevTools (F12) → Network tab → "
                "reload youtube.com → click any request → copy the 'Cookie' "
                "request header value (e.g. 'name1=val1; name2=val2').\n\n"
                "Note: cookies may expire over time and need to be refreshed. "
                "Leave empty for public content only."
            ),
            required=False,
        ),
    )
