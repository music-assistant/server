"""
Shared helper functions for the Deezer provider.

Utility functions used across multiple modules (parsers, browse, media, streaming).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import (
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    UniqueList,
)

from .constants import (
    AUDIOBOOK_CHAPTERS_PAGE_SIZE,
    FLOW_CONFIG_PREFIX,
    FLOW_PLAYLIST_ID,
    PERSONAL_SONGS_PLAYLIST_ID,
    RECOMMENDED_TRACKS_PLAYLIST_ID,
    SHAKER_CURATED_PREFIX,
    SHAKER_PREFIX,
    SMART_TRACKLIST_PREFIX,
    TOP_CHARTS_PLAYLIST_ID,
    USER_TOP_TRACKS_PLAYLIST_ID,
)

if TYPE_CHECKING:
    from deezer_python_gql import DeezerGQLClient
    from deezer_python_gql.generated.get_audiobook import (
        GetAudiobookAudiobookChaptersEdges,
        GetAudiobookAudiobookChaptersPageInfo,
    )

    from .provider import DeezerProvider


@dataclass(frozen=True)
class VirtualPlaylistMeta:
    """Canonical metadata for a virtual playlist type."""

    name: str
    is_dynamic: bool = False
    # translation_key for localizing the (fixed) display name; only set for exact-id
    # entries whose name is stable. Prefix-based entries carry dynamic names and stay None.
    translation_key: str | None = None


# Registry of virtual playlist types with their canonical name and is_dynamic flag.
# Keyed by exact playlist ID for fixed IDs, and by prefix for parameterized IDs.
VIRTUAL_PLAYLIST_TYPES: dict[str, VirtualPlaylistMeta] = {
    FLOW_PLAYLIST_ID: VirtualPlaylistMeta("Flow", is_dynamic=True, translation_key="flow"),
    FLOW_CONFIG_PREFIX: VirtualPlaylistMeta("Flow", is_dynamic=True),
    SMART_TRACKLIST_PREFIX: VirtualPlaylistMeta("Mix"),
    RECOMMENDED_TRACKS_PLAYLIST_ID: VirtualPlaylistMeta(
        "Hot Tracks", translation_key="recommended_tracks"
    ),
    TOP_CHARTS_PLAYLIST_ID: VirtualPlaylistMeta("Top Charts", translation_key="top_charts"),
    USER_TOP_TRACKS_PLAYLIST_ID: VirtualPlaylistMeta(
        "Your Top Tracks", translation_key="user_top_tracks"
    ),
    PERSONAL_SONGS_PLAYLIST_ID: VirtualPlaylistMeta("My Uploads", translation_key="personal_songs"),
    SHAKER_PREFIX: VirtualPlaylistMeta("Mix", is_dynamic=True),
    SHAKER_CURATED_PREFIX: VirtualPlaylistMeta("Playlist"),
}


def get_virtual_playlist_meta(item_id: str) -> VirtualPlaylistMeta | None:
    """
    Look up canonical metadata for a virtual playlist by its item_id.

    Tries exact match first, then longest prefix match.
    """
    if item_id in VIRTUAL_PLAYLIST_TYPES:
        return VIRTUAL_PLAYLIST_TYPES[item_id]
    # Sort by prefix length descending so longer prefixes match first
    # (e.g. "shaker_curated_" before "shaker_")
    for prefix, meta in sorted(
        VIRTUAL_PLAYLIST_TYPES.items(), key=lambda x: len(x[0]), reverse=True
    ):
        if prefix.endswith("_") and item_id.startswith(prefix):
            return meta
    return None


def create_virtual_playlist(
    provider: DeezerProvider,
    item_id: str,
    name: str,
    image_url: str | None = None,
    is_dynamic: bool | None = None,
) -> Playlist:
    """
    Create a virtual playlist for Flow, recommended content, etc.

    :param provider: The Deezer provider instance.
    :param item_id: The unique identifier (e.g., "flow", "smart_tracklist_123").
    :param name: Display name for the playlist.
    :param image_url: Optional cover image URL.
    :param is_dynamic: Whether the playlist returns fresh tracks on each fetch.
        If None, the value is looked up from the virtual playlist registry.
    """
    if is_dynamic is None:
        meta = get_virtual_playlist_meta(item_id)
        is_dynamic = meta.is_dynamic if meta else False
    # Only exact-id (fixed-name) virtual playlists are localizable; prefix-based ones
    # (flow configs, shaker groups, smart tracklists) carry dynamic names from the caller.
    exact_meta = VIRTUAL_PLAYLIST_TYPES.get(item_id)
    translation_key = exact_meta.translation_key if exact_meta else None
    images: UniqueList[MediaItemImage] = UniqueList()
    if image_url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )
    return Playlist(
        item_id=item_id,
        provider=provider.instance_id,
        name=name,
        media_type=MediaType.PLAYLIST,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
        metadata=MediaItemMetadata(images=images) if images else MediaItemMetadata(),
        is_editable=False,
        is_dynamic=is_dynamic,
        translation_key=translation_key,
        owner="Deezer",
    )


async def fetch_all_audiobook_chapter_edges(
    gql_client: DeezerGQLClient,
    audiobook_id: str,
    page_size: int = AUDIOBOOK_CHAPTERS_PAGE_SIZE,
    *,
    initial_edges: list[GetAudiobookAudiobookChaptersEdges] | None = None,
    initial_page_info: GetAudiobookAudiobookChaptersPageInfo | None = None,
) -> list[GetAudiobookAudiobookChaptersEdges]:
    """
    Paginate through all chapters of an audiobook and return the full edge list.

    :param gql_client: The Deezer GQL client to use.
    :param audiobook_id: The audiobook ID to fetch chapters for.
    :param page_size: Number of chapters per page.
    :param initial_edges: Pre-fetched edges to avoid re-fetching the first page.
    :param initial_page_info: Page info from the pre-fetched result.
    """
    if initial_edges is not None and initial_page_info is not None:
        all_edges = list(initial_edges)
        page_info = initial_page_info
    else:
        result = await gql_client.get_audiobook(audiobook_id=audiobook_id, chapters_first=page_size)
        if result is None:
            return []
        all_edges = list(result.chapters.edges)
        page_info = result.chapters.page_info
    while page_info.has_next_page:
        next_page = await gql_client.get_audiobook(
            audiobook_id=audiobook_id,
            chapters_first=page_size,
            chapters_after=page_info.end_cursor,
        )
        if next_page is None:
            break
        all_edges.extend(next_page.chapters.edges)
        page_info = next_page.chapters.page_info
    return all_edges


async def fetch_all_bookmarks(gql_client: DeezerGQLClient) -> dict[str, tuple[bool, int]]:
    """
    Paginate through all podcast episode bookmarks and return a lookup dict.

    :param gql_client: The Deezer GQL client to use.
    :returns: Dict mapping episode ID to (is_played, position_ms).
    """
    bookmarks: dict[str, tuple[bool, int]] = {}
    cursor: str | None = None
    while True:
        result = await gql_client.get_podcast_episode_bookmarks(first=50, after=cursor)
        if not result:
            break
        for edge in result.podcast_episode_bookmarks.edges:
            if edge.node is not None:
                bookmarks[edge.node.episode.id] = (
                    edge.node.is_played,
                    edge.node.position * 1000,
                )
        if not result.podcast_episode_bookmarks.page_info.has_next_page:
            break
        cursor = result.podcast_episode_bookmarks.page_info.end_cursor
    return bookmarks
