"""
Parsers for Deezer API response objects (GQL + GW).

Standalone functions that convert Deezer GQL Pydantic models and GW API
dicts into Music Assistant media item models.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from deezer_python_gql.generated.enums import AlbumType as DeezerAlbumType
from deezer_python_gql.generated.enums import AudiobookContributorRoles
from deezer_python_gql.generated.get_recently_played import (
    GetRecentlyPlayedMeRecentlyPlayedEdges,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeAlbum,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeArtist,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlow,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlowConfig,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodePlaylist,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeSmartTracklist,
)
from deezer_python_gql.generated.get_track import GetTrackTrack
from music_assistant_models.enums import (
    AlbumType,
    ExternalID,
    ImageType,
    MediaType,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)

from music_assistant.helpers.util import infer_album_type, parse_title_and_version

from .constants import (
    FLOW_CONFIG_PREFIX,
    FLOW_PLAYLIST_ID,
    PERSONAL_ALBUM_PREFIX,
    PERSONAL_ARTIST_PREFIX,
    SMART_TRACKLIST_PREFIX,
)
from .helpers import (
    create_virtual_playlist,
)

if TYPE_CHECKING:
    from deezer_python_gql.generated.fragments import (
        AlbumFields,
        ArtistFields,
        AudiobookFields,
        LivestreamFields,
        PlaylistFields,
        PodcastEpisodeFields,
        PodcastFields,
        TrackFields,
    )
    from deezer_python_gql.generated.get_audiobook import (
        GetAudiobookAudiobookChaptersEdges,
    )
    from deezer_python_gql.generated.get_flow_config_tracks import (
        GetFlowConfigTracksFlowConfig,
    )
    from deezer_python_gql.generated.get_flow_configs import (
        GetFlowConfigsMeFlowConfigsGenresEdgesNode,
        GetFlowConfigsMeFlowConfigsMoodsEdgesNode,
    )
    from deezer_python_gql.generated.search import (
        SearchSearchResultsAlbumsEdgesNode,
        SearchSearchResultsArtistsEdgesNode,
        SearchSearchResultsLivestreamsEdgesNode,
        SearchSearchResultsPlaylistsEdgesNode,
        SearchSearchResultsPodcastsEdgesNode,
        SearchSearchResultsTracksEdgesNode,
    )
    from deezer_python_gql.generated.search_flows import (
        SearchFlowsSearchResultsFlowConfigsEdgesNode,
    )

    from .provider import DeezerProvider

# Deezer CDN image URL pattern
DEEZER_CDN_IMAGE = "https://e-cdns-images.dzcdn.net/images"


# -- Protocols for typed GQL attributes --


class _CoverLike(Protocol):
    """Protocol for GQL cover/picture objects with a `.urls` list."""

    @property
    def urls(self) -> list[str]: ...


class _HasUrl(Protocol):
    """Protocol for GQL result objects with a `.url` field."""

    @property
    def url(self) -> object: ...


def _cover_image(provider: DeezerProvider, cover: _CoverLike | None) -> MediaItemImage | None:
    """
    Create a THUMB image from a GQL cover/picture object with a `.urls` list.

    Returns None when the cover is absent or has no URLs.
    """
    if cover and cover.urls:
        return MediaItemImage(
            type=ImageType.THUMB,
            path=cover.urls[0],
            provider=provider.instance_id,
            remotely_accessible=True,
        )
    return None


def _provider_mapping(
    provider: DeezerProvider,
    item_id: str,
    *,
    available: bool = True,
    is_unique: bool | None = None,
) -> ProviderMapping:
    """Create a ProviderMapping for the given item."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider.domain,
        provider_instance=provider.instance_id,
        available=available,
        is_unique=is_unique,
    )


# -- GQL model parsers --


def _track_available(
    track: TrackFields | SearchSearchResultsTracksEdgesNode | GetTrackTrack,
) -> bool:
    """Determine if a track is available for streaming."""
    media = track.media
    if media is None:
        return False
    if media.rights.sub is None:
        return False
    return bool(media.rights.sub.available)


def parse_track(
    provider: DeezerProvider,
    track: TrackFields | SearchSearchResultsTracksEdgesNode | GetTrackTrack,
    position: int = 0,
) -> Track:
    """
    Parse a GQL track model to Music Assistant Track.

    :param provider: The Deezer provider instance.
    :param track: A GQL track model (fragment-based or slim search result).
    :param position: Position in a track list (for playlist ordering).
    """
    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    for edge in track.contributors.edges:
        if edge.node is not None:
            artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=edge.node.id,
                    provider=provider.instance_id,
                    name=edge.node.name,
                )
            )

    album: Album | None = None
    if track.album is not None:
        album_images: UniqueList[MediaItemImage] = UniqueList()
        if img := _cover_image(provider, track.album.cover):
            album_images.append(img)
        # Use track contributors as album artists since the track-level album
        # sub-query doesn't include its own contributors.  This ensures the
        # album gets stored with artist references when added to the library.
        album_artists: UniqueList[Artist | ItemMapping] = UniqueList()
        for edge in track.contributors.edges:
            if edge.node is not None and edge.roles and "MAIN" in edge.roles:
                album_artists.append(
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=edge.node.id,
                        provider=provider.instance_id,
                        name=edge.node.name,
                    )
                )
        year: int | None = None
        if release_date := getattr(track, "release_date", None):
            if parsed := parse_date(str(release_date)):
                year = parsed.year
        album = Album(
            item_id=track.album.id,
            provider=provider.instance_id,
            name=track.album.display_title,
            year=year,
            artists=album_artists,
            provider_mappings={_provider_mapping(provider, track.album.id)},
            metadata=MediaItemMetadata(images=album_images)
            if album_images
            else MediaItemMetadata(),
        )

    name, version = parse_title_and_version(track.title)
    disc_number = 0
    track_number = position
    if (disk_info := getattr(track, "disk_info", None)) is not None:
        disc_number = disk_info.disk_number or 0
        track_number = disk_info.track_number or position

    item = Track(
        item_id=track.id,
        provider=provider.instance_id,
        name=name,
        version=version,
        duration=track.duration,
        artists=artists,
        album=album,
        provider_mappings={
            _provider_mapping(provider, track.id, available=_track_available(track))
        },
        metadata=_parse_track_metadata(provider, track),
        track_number=track_number,
        position=position,
        disc_number=disc_number,
    )
    if isrc := getattr(track, "isrc", None):
        item.external_ids.add((ExternalID.ISRC, isrc))
    if getattr(track, "is_favorite", False):
        item.favorite = True
    if (popularity := getattr(track, "popularity", None)) is not None:
        item.metadata.popularity = int(popularity)
    return item


def _parse_track_metadata(
    provider: DeezerProvider,
    track: TrackFields | SearchSearchResultsTracksEdgesNode | GetTrackTrack,
) -> MediaItemMetadata:
    """Parse track metadata (images, explicit flag, lyrics) from a GQL track model."""
    metadata = MediaItemMetadata(explicit=track.is_explicit)
    if track.album is not None:
        if img := _cover_image(provider, track.album.cover):
            metadata.add_image(img)
    # Lyrics (only present on GetTrackTrack, not on fragment-based models)
    if isinstance(track, GetTrackTrack) and track.lyrics is not None:
        if track.lyrics.text:
            metadata.lyrics = track.lyrics.text
        if track.lyrics.synchronized_lines:
            lrc_lines = [
                f"{line.lrc_timestamp} {line.line}"
                for line in track.lyrics.synchronized_lines
                if line.lrc_timestamp
            ]
            if lrc_lines:
                metadata.lrc_lyrics = "\n".join(lrc_lines)
    return metadata


def parse_artist(
    provider: DeezerProvider, artist: ArtistFields | SearchSearchResultsArtistsEdgesNode
) -> Artist:
    """Parse a GQL artist model to Music Assistant Artist."""
    images: UniqueList[MediaItemImage] = UniqueList()
    if img := _cover_image(provider, artist.picture):
        images.append(img)
    metadata = MediaItemMetadata(images=images) if images else MediaItemMetadata()
    if bio := getattr(artist, "bio", None):
        metadata.description = re.sub(r"<[^>]+>", "", bio.full).strip()
    if (fans_count := getattr(artist, "fans_count", None)) is not None:
        metadata.popularity = int(fans_count)
    item = Artist(
        item_id=artist.id,
        provider=provider.instance_id,
        name=artist.name,
        media_type=MediaType.ARTIST,
        provider_mappings={_provider_mapping(provider, artist.id)},
        metadata=metadata,
    )
    if getattr(artist, "is_favorite", False):
        item.favorite = True
    return item


def parse_album(
    provider: DeezerProvider, album: AlbumFields | SearchSearchResultsAlbumsEdgesNode
) -> Album:
    """Parse a GQL album model to Music Assistant Album."""
    name, version = parse_title_and_version(album.display_title)
    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    for edge in album.contributors.edges:
        if edge.node is not None:
            artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=edge.node.id,
                    provider=provider.instance_id,
                    name=edge.node.name,
                )
            )

    images: UniqueList[MediaItemImage] = UniqueList()
    if img := _cover_image(provider, album.cover):
        images.append(img)

    deezer_type = album.type_
    album_type = map_album_type(deezer_type, name)

    year: int | None = None
    if album.release_date:
        if parsed := parse_date(str(album.release_date)):
            year = parsed.year

    item = Album(
        album_type=album_type,
        item_id=album.id,
        provider=provider.instance_id,
        name=name,
        version=version,
        year=year,
        artists=artists,
        media_type=MediaType.ALBUM,
        provider_mappings={_provider_mapping(provider, album.id)},
        metadata=MediaItemMetadata(
            explicit=getattr(album, "is_explicit", None),
            images=images,
            label=getattr(album, "label", None),
            copyright=getattr(album, "copyright", None),
        ),
    )
    if (fans_count := getattr(album, "fans_count", None)) is not None:
        item.metadata.popularity = int(fans_count)
    if getattr(album, "is_favorite", False):
        item.favorite = True
    return item


def parse_playlist(
    provider: DeezerProvider,
    playlist: PlaylistFields | SearchSearchResultsPlaylistsEdgesNode,
    is_editable: bool = False,
) -> Playlist:
    """
    Parse a GQL playlist model to Music Assistant Playlist.

    :param provider: The Deezer provider instance.
    :param playlist: A GQL playlist model (fragment-based or slim search result).
    :param is_editable: Whether the current user owns this playlist.
    """
    images: UniqueList[MediaItemImage] = UniqueList()
    if img := _cover_image(provider, playlist.picture):
        images.append(img)
    owner_name = "Unknown"
    if playlist.owner:
        owner_name = playlist.owner.name
        if not is_editable and playlist.owner.id == provider.user_id:
            is_editable = True

    metadata = MediaItemMetadata(images=images) if images else MediaItemMetadata()
    if (fans_count := getattr(playlist, "fans_count", None)) is not None:
        metadata.popularity = int(fans_count)
    if description := getattr(playlist, "description", None):
        metadata.description = description
    item = Playlist(
        item_id=playlist.id,
        provider=provider.instance_id,
        name=playlist.title,
        media_type=MediaType.PLAYLIST,
        provider_mappings={_provider_mapping(provider, playlist.id, is_unique=is_editable)},
        metadata=metadata,
        is_editable=is_editable,
        owner=owner_name,
    )
    if getattr(playlist, "is_favorite", False) or is_editable:
        item.favorite = True
    return item


def parse_radio(
    provider: DeezerProvider, livestream: LivestreamFields | SearchSearchResultsLivestreamsEdgesNode
) -> Radio:
    """
    Parse a GQL Livestream model to Music Assistant Radio.

    :param provider: The Deezer provider instance.
    :param livestream: A GQL livestream model (fragment-based or slim search result).
    """
    images: UniqueList[MediaItemImage] = UniqueList()
    if img := _cover_image(provider, livestream.cover):
        images.append(img)
    metadata = MediaItemMetadata(images=images) if images else MediaItemMetadata()
    if description := getattr(livestream, "description", None):
        metadata.description = description
    return Radio(
        item_id=livestream.id,
        provider=provider.instance_id,
        name=livestream.name,
        media_type=MediaType.RADIO,
        provider_mappings={_provider_mapping(provider, livestream.id)},
        metadata=metadata,
    )


def parse_podcast(
    provider: DeezerProvider, podcast: PodcastFields | SearchSearchResultsPodcastsEdgesNode
) -> Podcast:
    """
    Parse a GQL podcast model to Music Assistant Podcast.

    :param provider: The Deezer provider instance.
    :param podcast: A GQL podcast model (fragment-based or slim search result).
    """
    images: UniqueList[MediaItemImage] = UniqueList()
    if img := _cover_image(provider, podcast.cover):
        images.append(img)
    metadata = MediaItemMetadata(
        images=images,
        explicit=getattr(podcast, "is_explicit", None),
    )
    if description := getattr(podcast, "description", None):
        metadata.description = description
    item = Podcast(
        item_id=podcast.id,
        provider=provider.instance_id,
        name=podcast.display_title,
        media_type=MediaType.PODCAST,
        provider_mappings={_provider_mapping(provider, podcast.id)},
        metadata=metadata,
    )
    if getattr(podcast, "is_favorite", False):
        item.favorite = True
    return item


def parse_podcast_episode(
    provider: DeezerProvider,
    episode: PodcastEpisodeFields,
    podcast: Podcast | ItemMapping,
    position: int = 0,
    podcast_image_url: str | None = None,
) -> PodcastEpisode:
    """
    Parse a GQL podcast episode model to Music Assistant PodcastEpisode.

    :param provider: The Deezer provider instance.
    :param episode: A GQL podcast episode model inheriting from PodcastEpisodeFields.
    :param podcast: Parent podcast reference.
    :param position: Sort position / episode number.
    :param podcast_image_url: Fallback image from parent podcast if episode has none.
    """
    images: UniqueList[MediaItemImage] = UniqueList()
    if img := _cover_image(provider, episode.cover):
        images.append(img)
    elif podcast_image_url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=podcast_image_url,
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )
    metadata = MediaItemMetadata(images=images)
    if episode.description:
        metadata.description = episode.description
    episode_name = episode.title
    if episode.publication_date:
        if pub_date := parse_date(str(episode.publication_date)):
            metadata.release_date = pub_date
            episode_name = f"{pub_date.strftime('%Y-%m-%d')} - {episode.title}"
    return PodcastEpisode(
        item_id=episode.id,
        provider=provider.instance_id,
        name=episode_name,
        duration=episode.duration,
        podcast=podcast,
        position=position,
        media_type=MediaType.PODCAST_EPISODE,
        provider_mappings={_provider_mapping(provider, episode.id)},
        metadata=metadata,
    )


def parse_audiobook(provider: DeezerProvider, audiobook: AudiobookFields) -> Audiobook:
    """
    Parse a GQL audiobook model to Music Assistant Audiobook.

    :param provider: The Deezer provider instance.
    :param audiobook: A GQL audiobook model inheriting from AudiobookFields.
    """
    images: UniqueList[MediaItemImage] = UniqueList()
    if img := _cover_image(provider, audiobook.cover):
        images.append(img)
    metadata = MediaItemMetadata(
        images=images,
        explicit=audiobook.is_explicit,
    )
    if audiobook.description:
        metadata.description = audiobook.description
    if audiobook.fans_count:
        metadata.popularity = int(audiobook.fans_count)

    authors: UniqueList[str | Artist | ItemMapping] = UniqueList()
    narrators: UniqueList[str | Artist | ItemMapping] = UniqueList()
    for edge in audiobook.contributors.edges:
        if AudiobookContributorRoles.NARRATOR in edge.roles:
            narrators.append(edge.node.name)
        else:
            authors.append(edge.node.name)

    item = Audiobook(
        item_id=audiobook.id,
        provider=provider.instance_id,
        name=audiobook.display_title or audiobook.id,
        duration=audiobook.duration,
        publisher=audiobook.publisher,
        authors=authors,
        narrators=narrators,
        media_type=MediaType.AUDIOBOOK,
        provider_mappings={_provider_mapping(provider, audiobook.id)},
        metadata=metadata,
    )
    if audiobook.is_favorite:
        item.favorite = True
    return item


def parse_audiobook_from_album(
    provider: DeezerProvider, album: AlbumFields | SearchSearchResultsAlbumsEdgesNode
) -> Audiobook:
    """
    Create an Audiobook from an AlbumFields result (search context).

    :param provider: The Deezer provider instance.
    :param album: A GQL album model (fragment-based or slim search result).
    """
    images: UniqueList[MediaItemImage] = UniqueList()
    if img := _cover_image(provider, album.cover):
        images.append(img)

    authors: UniqueList[str | Artist | ItemMapping] = UniqueList()
    for edge in album.contributors.edges:
        if edge.node is not None:
            authors.append(edge.node.name)

    return Audiobook(
        item_id=album.id,
        provider=provider.instance_id,
        name=album.display_title,
        authors=authors,
        media_type=MediaType.AUDIOBOOK,
        provider_mappings={_provider_mapping(provider, album.id)},
        metadata=MediaItemMetadata(
            images=images,
            explicit=getattr(album, "is_explicit", None),
        ),
    )


def parse_audiobook_chapters(
    chapter_edges: Sequence[GetAudiobookAudiobookChaptersEdges],
) -> list[MediaItemChapter]:
    """
    Build MediaItemChapter list from audiobook chapter edges.

    :param chapter_edges: List of chapter edge objects with `.node` attribute.
    """
    chapters: list[MediaItemChapter] = []
    cumulative_seconds = 0.0
    for idx, edge in enumerate(chapter_edges):
        if edge.node is None:
            continue
        duration = float(edge.node.duration)
        chapters.append(
            MediaItemChapter(
                position=idx + 1,
                name=edge.node.display_title,
                start=cumulative_seconds,
                end=cumulative_seconds + duration,
            )
        )
        cumulative_seconds += duration
    return chapters


def parse_recently_played_edges(
    provider: DeezerProvider,
    edges: list[GetRecentlyPlayedMeRecentlyPlayedEdges],
) -> list[MediaItemType]:
    """Parse recently played edges into MediaItemType list."""
    items: list[MediaItemType] = []
    for edge in edges:
        node = edge.node
        if node is None:
            continue
        if isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodeAlbum):
            items.append(parse_album(provider, node))
        elif isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodePlaylist):
            items.append(parse_playlist(provider, node))
        elif isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodeArtist):
            items.append(parse_artist(provider, node))
        elif isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlow):
            cover = node.cover.urls[0] if node.cover and node.cover.urls else None
            items.append(
                create_virtual_playlist(provider, FLOW_PLAYLIST_ID, node.title, image_url=cover)
            )
        elif isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlowConfig):
            cover = get_flow_config_image(node)
            playlist_id = f"{FLOW_CONFIG_PREFIX}{node.id}"
            items.append(
                create_virtual_playlist(
                    provider, playlist_id, f"Flow: {node.title}", image_url=cover
                )
            )
        elif isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodeSmartTracklist):
            cover = node.cover.urls[0] if node.cover and node.cover.urls else None
            items.append(
                create_virtual_playlist(
                    provider,
                    f"{SMART_TRACKLIST_PREFIX}{node.id}",
                    node.title,
                    image_url=cover,
                )
            )
    return items


# -- GW API parsers --


def parse_gw_item(provider: DeezerProvider, item: dict[str, Any]) -> MediaItemType | None:
    """Parse a GW page item to a Music Assistant media item."""
    item_type = item.get("type")
    data = item.get("data", {})
    try:
        if item_type == "album" and data.get("ALB_ID"):
            return parse_gw_audiobook(provider, data)
        if item_type == "playlist" and data.get("PLAYLIST_ID"):
            return parse_gw_playlist(provider, data)
        if item_type == "artist" and data.get("ART_ID"):
            return parse_gw_artist(provider, data)
    except KeyError:
        provider.logger.debug("Incomplete GW item data for type=%s, skipping", item_type)
    return None


def parse_gw_audiobook(provider: DeezerProvider, data: dict[str, Any]) -> Audiobook:
    """Parse a GW page album item to Music Assistant Audiobook."""
    album_id = str(data["ALB_ID"])
    title = data.get("ALB_TITLE", "")

    images: UniqueList[MediaItemImage] = UniqueList()
    if md5 := data.get("ALB_PICTURE"):
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=deezer_cover_url(md5, "cover"),
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )

    authors: UniqueList[str | Artist | ItemMapping] = UniqueList()
    if art_name := data.get("ART_NAME"):
        authors.append(art_name)

    return Audiobook(
        item_id=album_id,
        provider=provider.instance_id,
        name=title,
        authors=authors,
        media_type=MediaType.AUDIOBOOK,
        provider_mappings={_provider_mapping(provider, album_id)},
        metadata=MediaItemMetadata(images=images),
    )


def parse_gw_playlist(provider: DeezerProvider, data: dict[str, Any]) -> Playlist:
    """Parse a GW page playlist item to Music Assistant Playlist."""
    playlist_id = str(data["PLAYLIST_ID"])

    images: UniqueList[MediaItemImage] = UniqueList()
    if md5 := data.get("PLAYLIST_PICTURE"):
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=deezer_cover_url(md5, "playlist"),
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )

    metadata = MediaItemMetadata(images=images) if images else MediaItemMetadata()
    if desc := data.get("DESCRIPTION"):
        metadata.description = desc

    return Playlist(
        item_id=playlist_id,
        provider=provider.instance_id,
        name=data.get("TITLE", ""),
        media_type=MediaType.PLAYLIST,
        provider_mappings={_provider_mapping(provider, playlist_id)},
        metadata=metadata,
        owner=data.get("PARENT_USERNAME", "Deezer"),
    )


def parse_gw_artist(provider: DeezerProvider, data: dict[str, Any]) -> Artist:
    """Parse a GW page artist item to Music Assistant Artist."""
    artist_id = str(data["ART_ID"])

    images: UniqueList[MediaItemImage] = UniqueList()
    if md5 := data.get("ART_PICTURE"):
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=deezer_cover_url(md5, "artist"),
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )

    return Artist(
        item_id=artist_id,
        provider=provider.instance_id,
        name=data.get("ART_NAME", ""),
        media_type=MediaType.ARTIST,
        provider_mappings={_provider_mapping(provider, artist_id)},
        metadata=MediaItemMetadata(images=images),
    )


def parse_gw_track(provider: DeezerProvider, song: dict[str, Any], position: int = 0) -> Track:
    """
    Parse a GW API song dict into a Music Assistant Track.

    :param provider: The Deezer provider instance.
    :param song: Raw song dict from the GW API (personal_song.getList, etc.).
    :param position: Position in a track list.
    """
    song_id = str(song["SNG_ID"])
    is_personal = int(song_id) < 0
    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    art_name = song.get("ART_NAME", "")
    art_id = str(song.get("ART_ID", "0"))
    if art_name:
        if is_personal or art_id == "0":
            # Personal tracks have ART_ID=0 which doesn't exist on Deezer.
            # Create a full Artist object so MA doesn't try to resolve it.
            # Use a prefixed ID to avoid collisions with the track's provider mapping.
            personal_art_id = f"{PERSONAL_ARTIST_PREFIX}{song_id}"
            artists.append(
                Artist(
                    item_id=personal_art_id,
                    provider=provider.instance_id,
                    name=art_name,
                    favorite=True,
                    provider_mappings={_provider_mapping(provider, personal_art_id)},
                )
            )
        else:
            artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=art_id,
                    provider=provider.instance_id,
                    name=art_name,
                )
            )

    album: Album | ItemMapping | None = None
    alb_title = song.get("ALB_TITLE", "")
    alb_id = str(song.get("ALB_ID", 0))
    if alb_title:
        if is_personal or alb_id == "0":
            # Personal tracks have ALB_ID=0 which doesn't exist on Deezer.
            # Create a full Album object so MA doesn't try to resolve it.
            # Use a prefixed ID to avoid collisions with the track's provider mapping.
            personal_alb_id = f"{PERSONAL_ALBUM_PREFIX}{song_id}"
            album = Album(
                item_id=personal_alb_id,
                provider=provider.instance_id,
                name=alb_title,
                favorite=True,
                artists=artists,
                provider_mappings={_provider_mapping(provider, personal_alb_id)},
            )
        else:
            album = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=alb_id,
                provider=provider.instance_id,
                name=alb_title,
            )

    name, version = parse_title_and_version(song.get("SNG_TITLE", ""))
    return Track(
        item_id=song_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        duration=int(song.get("DURATION", 0)),
        favorite=is_personal,
        artists=artists,
        album=album,
        provider_mappings={_provider_mapping(provider, song_id, available=True)},
        position=position,
    )


# -- Helper functions --


def deezer_cover_url(md5: str, image_type: str = "cover", size: int = 500) -> str:
    """Construct a Deezer CDN image URL from an MD5 hash."""
    return f"{DEEZER_CDN_IMAGE}/{image_type}/{md5}/{size}x{size}-000000-80-0-0.jpg"


def get_flow_config_image(
    node: GetFlowConfigTracksFlowConfig
    | GetFlowConfigsMeFlowConfigsMoodsEdgesNode
    | GetFlowConfigsMeFlowConfigsGenresEdgesNode
    | SearchFlowsSearchResultsFlowConfigsEdgesNode
    | GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlowConfig,
) -> str | None:
    """Extract the square icon URL from a FlowConfig node's visuals."""
    icon = node.visuals.hardware_square_icon
    if icon and icon.urls:
        url: str = icon.urls[0]
        return url
    return None


def get_gw_item_image(provider: DeezerProvider, item: dict[str, Any]) -> MediaItemImage | None:
    """Extract a cover image from a GW page item."""
    data = item.get("data", {})
    item_type = item.get("type")
    md5: str | None = None
    img_type = "cover"

    if item_type == "album":
        md5 = data.get("ALB_PICTURE")
    elif item_type == "playlist":
        md5 = data.get("PLAYLIST_PICTURE")
        img_type = "playlist"
    elif item_type == "artist":
        md5 = data.get("ART_PICTURE")
        img_type = "artist"
    elif item_type == "channel":
        pictures = item.get("pictures", [])
        if pictures:
            md5 = pictures[0].get("md5")
            img_type = pictures[0].get("type", "misc")

    if md5:
        return MediaItemImage(
            type=ImageType.THUMB,
            path=deezer_cover_url(md5, img_type),
            provider=provider.instance_id,
            remotely_accessible=True,
        )
    return None


def apply_web_url(item: Artist | Album, gql_result: _HasUrl) -> None:
    """Set web URL on provider mappings if available in the GQL result."""
    if web_url := getattr(gql_result.url, "web_url", None):
        for pm in item.provider_mappings:
            pm.url = web_url


def map_album_type(deezer_type: DeezerAlbumType | None, title: str) -> AlbumType:
    """Map Deezer album type to Music Assistant AlbumType."""
    inferred = infer_album_type(title, "")
    if inferred in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
        return inferred
    if deezer_type is None:
        return AlbumType.UNKNOWN
    match deezer_type:
        case DeezerAlbumType.ALBUM:
            return AlbumType.ALBUM
        case DeezerAlbumType.SINGLES:
            return AlbumType.SINGLE
        case DeezerAlbumType.EP:
            return AlbumType.EP
        case DeezerAlbumType.COMPILATIONS:
            return AlbumType.COMPILATION
        case _:
            return AlbumType.UNKNOWN


def parse_date(date_value: str | None) -> datetime | None:
    """Parse a date value from the GQL API to a timezone-aware datetime."""
    try:
        return datetime.fromisoformat(str(date_value))
    except ValueError, TypeError:
        return None
