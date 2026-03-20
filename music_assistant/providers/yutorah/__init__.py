"""YuTorah music provider for Music Assistant.

Streams Torah shiurim (audio lectures) from YUTorah Online (yutorah.org)
using their native mobile app JSON API (discovered via APK reverse engineering).

Data model mapping:
  Series (e.g. "Daf Yomi", "Ten Minute Halacha")  →  Podcast
  Shiur within that series                         →  PodcastEpisode (direct MP3)

API base: https://yutorah.org/api/
Login is required and unlocks the full paginated episode lists and full search.

Authenticated endpoints (require userToken query param, obtained via login):
  POST login/default {email, password}       → {"loginSuccess": true, "userToken": "..."}
  GET search/get?searchTerm=&seriesID=X&getFacets=true&userToken=T   → page 1 (30 results)
  GET search/get?searchTerm=&seriesID=X&getFacets=false&start=N&userToken=T → page N (N≥2)
  GET search/get?searchTerm=QUERY&getFacets=true&userToken=T          → full-text search

  IMPORTANT: searchTerm must always be present (even as empty string). Omitting it or
  sending only seriesID/teacherID/subcategoryID without searchTerm returns "". The start
  parameter is a 1-based PAGE NUMBER (not an offset), and must be omitted on the first
  request. Page size is 30 results per page.

  search/get response shape (Solr-style):
    {
      "response": {"docs": [...shiur objects...], "numFound": N},
      "facet_counts": {"facet_fields": {"teachers": [...], "series": [...]}}
    }

Browse path structure:
  yutorah://                                         → root folders
  yutorah://series                                   → all series (as folders)
  yutorah://series/<series_id>                       → teacher sub-folders within a series
  yutorah://series/<series_id>/teacher/<teacher_id>  → episodes for a teacher within a series
  yutorah://teachers                                 → all teachers
  yutorah://teachers/<teacher_id>                    → all episodes by a teacher
  yutorah://categories                               → topic category tree
  yutorah://categories/<subcategory_id>              → series in a category
  yutorah://recent                                   → 50 most recent shiurim
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME

from .constants import SUPPORTED_FEATURES
from .provider import YuTorahProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant

__all__ = ["SUPPORTED_FEATURES", "YuTorahProvider", "get_config_entries", "setup"]


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key="label_auth",
            type=ConfigEntryType.LABEL,
            label="A free YuTorah account is required. Sign up at yutorah.org.",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Email address",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> YuTorahProvider:
    """Initialize provider instance with the given configuration."""
    return YuTorahProvider(mass, manifest, config, SUPPORTED_FEATURES)
