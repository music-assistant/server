"""Parsers for Yandex Music API responses."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    ImageType,
)
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.helpers.util import parse_title_and_version

from .constants import (
    IMAGE_SIZE_LARGE,
    PROVIDER_DISPLAY_NAME_EN,
    PROVIDER_DISPLAY_NAME_RU,
    WEB_BASE_URL,
    YANDEX_SYSTEM_OWNER_NAMES,
)

if TYPE_CHECKING:
    from yandex_music import Album as YandexAlbum
    from yandex_music import Artist as YandexArtist
    from yandex_music import Playlist as YandexPlaylist
    from yandex_music import Track as YandexTrack

    from .provider import YandexMusicProvider


AlbumKind = Literal["music", "podcast", "audiobook"]


def classify_album(album_obj: YandexAlbum) -> AlbumKind:
    """
    Classify a Yandex album as music / podcast / audiobook.

    Checks both ``meta_type`` and ``type`` for the substrings "audiobook" /
    "podcast". The more specific "audiobook" signal wins over "podcast" on any
    field because Yandex tags audiobooks with ``meta_type="podcast"`` *and*
    ``type="audiobook"`` — empirically observed in production libraries.
    Values are not documented in the yandex_music SDK.

    :param album_obj: Yandex album object.
    :return: One of "music", "podcast", "audiobook".
    """
    fields = [
        (getattr(album_obj, "meta_type", None) or "").lower(),
        (getattr(album_obj, "type", None) or "").lower(),
    ]
    if any("audiobook" in f for f in fields):
        return "audiobook"
    if any("podcast" in f for f in fields):
        return "podcast"
    return "music"


def get_canonical_provider_name(provider: YandexMusicProvider) -> str:
    """
    Return the locale-aware canonical display name for the Yandex Music system account.

    :param provider: The Yandex Music provider instance.
    :return: Localized provider display name.
    """
    with suppress(Exception):
        locale = (provider.mass.metadata.locale or "en_US").lower()
        if locale.startswith("ru"):
            return PROVIDER_DISPLAY_NAME_RU
    return PROVIDER_DISPLAY_NAME_EN


def _get_image_url(cover_uri: str | None, size: str = IMAGE_SIZE_LARGE) -> str | None:
    """
    Convert Yandex cover URI to full URL.

    :param cover_uri: Yandex cover URI template.
    :param size: Image size (e.g., '1000x1000').
    :return: Full image URL or None.
    """
    if not cover_uri:
        return None
    # Cover URIs come in format "avatars.yandex.net/get-music-content/xxx/yyy/%%"
    # Replace %% with the desired size
    return f"https://{cover_uri.replace('%%', size)}"


_NON_RUSSIAN_CYRILLIC_MARKERS = frozenset("їєґіўЇЄҐІЎ")


def detect_description_language(text: str | None) -> Literal["ru"] | None:
    """
    Return ``"ru"`` for Russian-language text, ``None`` otherwise.

    Yandex Music's API does not expose the language of artist / playlist /
    podcast descriptions, so we infer it from script. A string classifies as
    Russian when it (a) contains at least 8 Cyrillic characters that
    (b) make up at least 50% of its length and (c) contains none of the
    letters that mark another Slavic Cyrillic language (see
    ``_NON_RUSSIAN_CYRILLIC_MARKERS`` — currently Ukrainian and Belarusian
    discriminators). Everything else returns ``None`` so MA can fall back
    to metadata providers for a user-localized bio.

    :param text: The description string to classify.
    :return: ``"ru"`` when the heuristic is confident, ``None`` otherwise.
    """
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    if not _NON_RUSSIAN_CYRILLIC_MARKERS.isdisjoint(text):
        return None
    cyrillic = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
    # Floor + 50% share: a stray transliterated word in an English bio (e.g.
    # an artist's Cyrillic name) must not flip the result to "ru".
    if cyrillic >= 8 and cyrillic * 2 >= len(text):
        return "ru"
    return None


def parse_artist(
    provider: YandexMusicProvider,
    artist_obj: YandexArtist,
    *,
    about: object | None = None,
) -> Artist:
    """
    Parse Yandex artist object to MA Artist model.

    :param provider: The Yandex Music provider instance.
    :param artist_obj: Yandex artist object.
    :param about: Optional ArtistAbout enrichment (description + listener stats).
    :return: Music Assistant Artist model.
    """
    if artist_obj.id is None:
        raise InvalidDataError("Yandex artist missing id")
    artist_id = str(artist_obj.id)
    artist = Artist(
        item_id=artist_id,
        provider=provider.instance_id,
        name=artist_obj.name or "Unknown Artist",
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"{WEB_BASE_URL}/artist/{artist_id}",
            )
        },
    )

    # Add image if available
    if artist_obj.cover:
        image_url = _get_image_url(artist_obj.cover.uri)
        if image_url:
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
    elif artist_obj.og_image:
        image_url = _get_image_url(artist_obj.og_image)
        if image_url:
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

    if about is not None:
        description = getattr(about, "description", None)
        if description:
            artist.metadata.description = description
            artist.metadata.description_language = detect_description_language(description)
        stats = getattr(about, "stats", None)
        monthly = getattr(stats, "last_month_listeners", None) if stats else None
        if monthly is not None:
            artist.metadata.popularity = max(0, min(100, monthly // 10000))

    return artist


def _album_cover_images(
    provider: YandexMusicProvider, album_obj: YandexAlbum
) -> UniqueList[MediaItemImage]:
    """
    Build the UniqueList of images for an album-like object.

    Prefers the templated ``cover_uri`` and falls back to ``og_image`` — matches
    the selection rules used for podcasts and audiobooks so all album-like
    parsers stay in sync.
    """
    images: UniqueList[MediaItemImage] = UniqueList()
    image_url: str | None = None
    if album_obj.cover_uri:
        image_url = _get_image_url(album_obj.cover_uri)
    elif album_obj.og_image:
        image_url = _get_image_url(album_obj.og_image)
    if image_url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )
    return images


def parse_album(provider: YandexMusicProvider, album_obj: YandexAlbum) -> Album:
    """
    Parse Yandex album object to MA Album model.

    :param provider: The Yandex Music provider instance.
    :param album_obj: Yandex album object.
    :return: Music Assistant Album model.
    """
    if album_obj.id is None:
        raise InvalidDataError("Yandex album missing id")
    name, version = parse_title_and_version(
        album_obj.title or "Unknown Album",
        album_obj.version or None,
    )
    album_id = str(album_obj.id)

    # Determine availability
    available = album_obj.available or False

    album = Album(
        item_id=album_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.UNKNOWN,
                ),
                url=f"{WEB_BASE_URL}/album/{album_id}",
                available=available,
            )
        },
    )

    # Parse artists
    various_artist_album = False
    if album_obj.artists:
        for artist in album_obj.artists:
            if artist.name and artist.name.lower() in ("various artists", "сборник"):
                various_artist_album = True
            album.artists.append(parse_artist(provider, artist))

    # Determine album type
    album_type_str = album_obj.type or "album"
    if album_type_str == "compilation" or various_artist_album:
        album.album_type = AlbumType.COMPILATION
    elif album_type_str == "single":
        album.album_type = AlbumType.SINGLE
    else:
        album.album_type = AlbumType.ALBUM

    # Parse year
    if album_obj.year:
        album.year = album_obj.year
    if album_obj.release_date:
        with suppress(ValueError):
            album.metadata.release_date = datetime.fromisoformat(album_obj.release_date)

    # Parse metadata
    if album_obj.genre:
        album.metadata.genres = {album_obj.genre}

    images = _album_cover_images(provider, album_obj)
    if images:
        album.metadata.images = images

    return album


def parse_track(
    provider: YandexMusicProvider,
    track_obj: YandexTrack,
    lyrics: str | None = None,
    lyrics_synced: bool = False,
) -> Track:
    """
    Parse Yandex track object to MA Track model.

    :param provider: The Yandex Music provider instance.
    :param track_obj: Yandex track object.
    :param lyrics: Optional lyrics text.
    :param lyrics_synced: Whether lyrics are in synced LRC format.
    :return: Music Assistant Track model.
    """
    if track_obj.id is None:
        raise InvalidDataError("Yandex track missing id")
    name, version = parse_title_and_version(
        track_obj.title or "Unknown Track",
        track_obj.version or None,
    )
    track_id = str(track_obj.id)

    # Determine availability
    available = track_obj.available or False

    # Duration is in milliseconds in Yandex API
    duration = (track_obj.duration_ms or 0) // 1000

    track = Track(
        item_id=track_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        duration=duration,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.UNKNOWN,
                ),
                url=f"{WEB_BASE_URL}/track/{track_id}",
                available=available,
            )
        },
    )

    # Parse artists
    if track_obj.artists:
        track.artists = UniqueList()
        for artist in track_obj.artists:
            track.artists.append(parse_artist(provider, artist))

    # Parse album (full data so album gets cover art in the library)
    if track_obj.albums and len(track_obj.albums) > 0:
        album_obj = track_obj.albums[0]
        track.album = parse_album(provider, album_obj)
        # Also set track image from album cover if available
        if album_obj.cover_uri:
            image_url = _get_image_url(album_obj.cover_uri)
            if image_url:
                track.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=image_url,
                            provider=provider.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )

    # Metadata
    if track_obj.content_warning:
        track.metadata.explicit = track_obj.content_warning == "explicit"

    # Lyrics
    if lyrics:
        if lyrics_synced:
            track.metadata.lrc_lyrics = lyrics
        else:
            track.metadata.lyrics = lyrics

    return track


def parse_playlist(
    provider: YandexMusicProvider,
    playlist_obj: YandexPlaylist,
    owner_name: str | None = None,
    *,
    is_dynamic: bool = False,
) -> Playlist:
    """
    Parse Yandex playlist object to MA Playlist model.

    :param provider: The Yandex Music provider instance.
    :param playlist_obj: Yandex playlist object.
    :param owner_name: Optional owner name override.
    :param is_dynamic: Mark the playlist as dynamic so Music Assistant does
        not long-cache its content. Yandex regenerates "Playlist of the Day",
        "DejaVu", "Premiere" etc. on a schedule, and those need a fresh read
        on every browse so users actually see the updated selection.
    :return: Music Assistant Playlist model.
    """
    # Playlist ID in Yandex is a combination of owner uid and playlist kind
    owner_id = str(playlist_obj.owner.uid) if playlist_obj.owner else str(provider.client.user_id)
    playlist_kind = str(playlist_obj.kind)
    playlist_id = f"{owner_id}:{playlist_kind}"

    # Determine if editable (user owns the playlist)
    is_editable = owner_id == str(provider.client.user_id)

    # Get owner name
    if owner_name is None:
        if playlist_obj.owner and playlist_obj.owner.name:
            owner_name = playlist_obj.owner.name
        elif is_editable:
            owner_name = "Me"
        else:
            owner_name = get_canonical_provider_name(provider)

    # Normalize all known system account name variants to locale-aware canonical form
    if owner_name and owner_name.lower() in YANDEX_SYSTEM_OWNER_NAMES:
        owner_name = get_canonical_provider_name(provider)

    playlist = Playlist(
        item_id=playlist_id,
        provider=provider.instance_id,
        name=playlist_obj.title or "Unknown Playlist",
        owner=owner_name,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"{WEB_BASE_URL}/users/{owner_id}/playlists/{playlist_kind}",
                is_unique=is_editable,
            )
        },
        is_editable=is_editable,
        is_dynamic=is_dynamic,
    )

    # Metadata
    if playlist_obj.description:
        playlist.metadata.description = playlist_obj.description

    # Add cover image
    if playlist_obj.cover:
        # Cover can be CoverImage or a string
        cover = playlist_obj.cover
        if hasattr(cover, "uri") and cover.uri:
            image_url = _get_image_url(cover.uri)
            if image_url:
                playlist.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=image_url,
                            provider=provider.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
    elif playlist_obj.og_image:
        image_url = _get_image_url(playlist_obj.og_image)
        if image_url:
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

    return playlist


def parse_podcast(provider: YandexMusicProvider, album_obj: YandexAlbum) -> Podcast:
    """
    Parse Yandex album (meta_type=podcast) to MA Podcast model.

    :param provider: The Yandex Music provider instance.
    :param album_obj: Yandex album object classified as a podcast.
    :return: Music Assistant Podcast model.
    """
    if album_obj.id is None:
        raise InvalidDataError("Yandex podcast missing id")
    name, _ = parse_title_and_version(
        album_obj.title or "Unknown Podcast",
        album_obj.version or None,
    )
    podcast_id = str(album_obj.id)
    available = album_obj.available or False

    # Publisher: prefer labels[0].name; fall back to first artist name
    publisher: str | None = None
    labels = getattr(album_obj, "labels", None)
    if labels:
        first = labels[0]
        label_name = getattr(first, "name", None) if not isinstance(first, str) else first
        if label_name:
            publisher = label_name
    if not publisher and album_obj.artists:
        first_artist = album_obj.artists[0]
        if first_artist.name:
            publisher = first_artist.name

    podcast = Podcast(
        item_id=podcast_id,
        provider=provider.instance_id,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=podcast_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                url=f"{WEB_BASE_URL}/album/{podcast_id}",
                available=available,
            )
        },
        publisher=publisher,
        total_episodes=album_obj.track_count,
    )

    description = album_obj.description or album_obj.short_description
    if description:
        podcast.metadata.description = description
    if album_obj.content_warning:
        podcast.metadata.explicit = album_obj.content_warning == "explicit"

    images = _album_cover_images(provider, album_obj)
    if images:
        podcast.metadata.images = images

    if album_obj.genre:
        podcast.metadata.genres = {album_obj.genre}
    else:
        podcast.metadata.genres = {"Spoken Word"}

    if album_obj.release_date:
        with suppress(ValueError):
            podcast.metadata.release_date = datetime.fromisoformat(album_obj.release_date)

    return podcast


def parse_podcast_episode(
    provider: YandexMusicProvider,
    track_obj: YandexTrack,
    podcast: Podcast,
    position: int = 0,
) -> PodcastEpisode:
    """
    Parse Yandex track (episode of a podcast album) to MA PodcastEpisode.

    :param provider: The Yandex Music provider instance.
    :param track_obj: Yandex track object.
    :param podcast: Parent Podcast object.
    :param position: 1-based episode index (0 if unknown).
    :return: Music Assistant PodcastEpisode model.
    """
    if track_obj.id is None:
        raise InvalidDataError("Yandex podcast episode missing id")
    episode_id = str(track_obj.id)
    available = track_obj.available or False
    duration = (track_obj.duration_ms or 0) // 1000

    episode_name = track_obj.title or (f"Episode {position}" if position else "Unknown Episode")
    episode = PodcastEpisode(
        item_id=episode_id,
        provider=provider.instance_id,
        name=episode_name,
        duration=duration,
        podcast=podcast,
        position=position,
        provider_mappings={
            ProviderMapping(
                item_id=episode_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                url=f"{WEB_BASE_URL}/track/{episode_id}",
                available=available,
            )
        },
    )

    if track_obj.short_description:
        episode.metadata.description = track_obj.short_description
    if track_obj.content_warning:
        episode.metadata.explicit = track_obj.content_warning == "explicit"

    # Track cover → fall back to podcast cover
    if track_obj.cover_uri:
        image_url = _get_image_url(track_obj.cover_uri)
        if image_url:
            episode.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
    elif track_obj.og_image:
        image_url = _get_image_url(track_obj.og_image)
        if image_url:
            episode.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
    if not episode.metadata.images and podcast.metadata.images:
        episode.metadata.images = UniqueList(podcast.metadata.images)

    return episode


def parse_audiobook(provider: YandexMusicProvider, album_obj: YandexAlbum) -> Audiobook:
    """
    Parse Yandex album (meta_type=audiobook) to MA Audiobook model.

    :param provider: The Yandex Music provider instance.
    :param album_obj: Yandex album object classified as an audiobook.
    :return: Music Assistant Audiobook model. Chapters and duration are filled
        by the provider's get_audiobook() method after loading album tracks.
    """
    if album_obj.id is None:
        raise InvalidDataError("Yandex audiobook missing id")
    name, _ = parse_title_and_version(
        album_obj.title or "Unknown Audiobook",
        album_obj.version or None,
    )
    audiobook_id = str(album_obj.id)
    available = album_obj.available or False

    # Publisher: prefer labels[0]; fall back to nothing (authors sit on artists)
    publisher: str | None = None
    labels = getattr(album_obj, "labels", None)
    if labels:
        first = labels[0]
        label_name = getattr(first, "name", None) if not isinstance(first, str) else first
        if label_name:
            publisher = label_name

    authors: UniqueList[str | Artist | ItemMapping] = UniqueList()
    if album_obj.artists:
        for artist in album_obj.artists:
            if artist.name:
                authors.append(artist.name)

    audiobook = Audiobook(
        item_id=audiobook_id,
        provider=provider.instance_id,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=audiobook_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                url=f"{WEB_BASE_URL}/album/{audiobook_id}",
                available=available,
            )
        },
        publisher=publisher,
        authors=authors,
        narrators=UniqueList(),
        duration=0,
    )

    description = album_obj.description or album_obj.short_description
    if description:
        audiobook.metadata.description = description
    if album_obj.content_warning:
        audiobook.metadata.explicit = album_obj.content_warning == "explicit"

    images = _album_cover_images(provider, album_obj)
    if images:
        audiobook.metadata.images = images

    if album_obj.genre:
        audiobook.metadata.genres = {album_obj.genre}
    else:
        audiobook.metadata.genres = {"Spoken Word"}

    if album_obj.release_date:
        with suppress(ValueError):
            audiobook.metadata.release_date = datetime.fromisoformat(album_obj.release_date)

    listening_finished = getattr(album_obj, "listening_finished", None)
    if listening_finished is not None:
        audiobook.fully_played = bool(listening_finished)

    return audiobook
