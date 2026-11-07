"""
Explicit content filtering for the Deezer provider.

Deezer keeps an explicit content level on every account and profile, but only its own apps
apply it: the APIs hand explicit tracks to any client. This module reads that level and decides
which items the provider leaves out, so a child's account behaves in Music Assistant the way it
does in the Deezer apps.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from deezer_python_gql import DeezerGQLClient

CONF_EXPLICIT_CONTENT = "explicit_content"
CONF_HIDE_EXPLICIT_ALBUMS = "hide_explicit_albums"

# the media types whose explicit flag hides them; an album is only hidden as a whole when
# asked, as Deezer flags it explicit as soon as one of its tracks is
_FILTERED_MEDIA_TYPES = frozenset(
    {
        MediaType.TRACK,
        MediaType.PODCAST,
        MediaType.PODCAST_EPISODE,
        MediaType.AUDIOBOOK,
    }
)

# EXPLICIT_LYRICS_STATUS values of a GW song that mark it explicit
_GW_EXPLICIT_STATUSES = {1, 4}

_EXPLICIT_LEVEL_QUERY = """
query GetExplicitLevel {
  me {
    explicitLevel
  }
}
"""


class ExplicitContentSetting(StrEnum):
    """The explicit content option of a Deezer provider instance."""

    ACCOUNT = "account"
    HIDE = "hide"
    NO_RECOMMENDATIONS = "no_recommendations"
    SHOW = "show"


class ExplicitMode(StrEnum):
    """How explicit content is handled once the setting is resolved."""

    HIDE = "hide"
    NO_RECOMMENDATIONS = "no_recommendations"
    SHOW = "show"


# Deezer's UserExplicitLevel, as set under Settings > Explicit content on deezer.com
_ACCOUNT_LEVELS = {
    "EXPLICIT_HIDE": ExplicitMode.HIDE,
    "EXPLICIT_NO_RECOMMENDATION": ExplicitMode.NO_RECOMMENDATIONS,
    "EXPLICIT_DISPLAY": ExplicitMode.SHOW,
}


async def fetch_account_explicit_mode(client: DeezerGQLClient) -> ExplicitMode | None:
    """
    Return the explicit content level of the signed-in account or profile.

    None means Deezer returned a level this provider does not know.

    :param client: The GraphQL client of the account or Family profile.
    """
    response = await client.execute(
        query=_EXPLICIT_LEVEL_QUERY, operation_name="GetExplicitLevel", variables={}
    )
    me = client.get_data(response).get("me") or {}
    return _ACCOUNT_LEVELS.get(str(me.get("explicitLevel")))


def filtered_media_types(hide_albums: bool) -> frozenset[MediaType]:
    """
    Return the media types the filter hides when they are explicit.

    :param hide_albums: Whether explicit albums are hidden as a whole as well.
    """
    return _FILTERED_MEDIA_TYPES | {MediaType.ALBUM} if hide_albums else _FILTERED_MEDIA_TYPES


def is_explicit(item: Any, media_types: Collection[MediaType] = _FILTERED_MEDIA_TYPES) -> bool:
    """
    Return whether an item is explicit content the filter hides.

    :param item: A media item, item mapping or browse folder.
    :param media_types: The media types the filter hides when they are explicit.
    """
    if getattr(item, "media_type", None) not in media_types:
        return False
    metadata = getattr(item, "metadata", None)
    return bool(metadata is not None and metadata.explicit)


def without_explicit[T](
    items: Iterable[T], media_types: Collection[MediaType] = _FILTERED_MEDIA_TYPES
) -> list[T]:
    """
    Return the items that are not explicit content, in their original order.

    :param items: The items to filter.
    :param media_types: The media types the filter hides when they are explicit.
    """
    return [item for item in items if not is_explicit(item, media_types)]


def gw_song_is_explicit(song: dict[str, Any]) -> bool:
    """
    Return whether a GW song.getData result is explicit.

    :param song: The song data returned by the GW API.
    """
    content = song.get("EXPLICIT_TRACK_CONTENT") or {}
    try:
        status = int(content.get("EXPLICIT_LYRICS_STATUS", -1))
        lyrics_flag = int(song.get("EXPLICIT_LYRICS") or 0)
    except TypeError, ValueError:
        return False
    return status in _GW_EXPLICIT_STATUSES or lyrics_flag == 1
