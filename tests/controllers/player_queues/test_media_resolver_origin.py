"""
Tests for the origin a resolved item records: where it was played from, and which copy to play.

The resolver wraps every item a container resolves to with that container and, where the
listing had one specific copy of the item, with the (provider instance, item id) of that copy,
so playback can prefer the copy the user actually picked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.player_queues.media_resolver import MediaResolver, ResolvedItem

TIDAL = "tidal--abc"
SPOTIFY = "spotify--def"
GPODDER = "gpodder--ghi"
FILESYSTEM = "filesystem_local--xyz"
FOLDER_PATH = "Music/Miles Davis/Kind of Blue"


def _mapping(instance: str, item_id: str) -> ProviderMapping:
    """Build a mapping of an item on the given provider instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance.split("--", maxsplit=1)[0],
        provider_instance=instance,
    )


def _track(item_id: str, provider: str, *mappings: ProviderMapping) -> Track:
    """Build a track; a provider track maps to itself unless mappings are given."""
    return Track(
        item_id=item_id,
        provider=provider,
        name=item_id,
        provider_mappings=set(mappings) or {_mapping(provider, item_id)},
    )


def _playlist(provider: str, *mappings: ProviderMapping) -> Playlist:
    """Build a playlist; a provider playlist maps to itself unless mappings are given."""
    return Playlist(
        item_id="pl1",
        provider=provider,
        name="Jazz",
        provider_mappings=set(mappings) or {_mapping(provider, "pl1")},
    )


def _resolver() -> MediaResolver:
    """Build a bare resolver whose container lookups are stubbed per test."""
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.logger = Mock()
    resolver.mass = Mock()
    resolver.mass.create_task = Mock(side_effect=lambda coro: coro)
    resolver.mass.music.mark_item_played = Mock()
    resolver.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    return resolver


def _container(resolved: ResolvedItem) -> ItemMapping:
    """Return the container a resolved item was played from."""
    assert resolved.origin is not None
    assert resolved.origin.container is not None
    return resolved.origin.container


def _pin(resolved: ResolvedItem) -> tuple[str | None, str | None]:
    """Return the (provider instance, item id) a resolved item is pinned to."""
    assert resolved.origin is not None
    return (resolved.origin.provider_instance, resolved.origin.item_id)


async def test_a_provider_playlist_pins_each_track_to_its_entry() -> None:
    """A playlist of a streaming service plays every track from that service's listing."""
    resolver = _resolver()
    playlist = _playlist(TIDAL)
    tracks = [_track("t1", TIDAL), _track("t2", TIDAL)]
    resolver.get_playlist_tracks = AsyncMock(return_value=tracks)  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(playlist)

    assert [x.item for x in resolved] == tracks
    assert [_container(x) for x in resolved] == [ItemMapping.from_item(playlist)] * 2
    assert _container(resolved[0]).media_type == MediaType.PLAYLIST
    assert [_pin(x) for x in resolved] == [(TIDAL, "t1"), (TIDAL, "t2")]


@pytest.mark.parametrize(
    "playlist",
    [
        _playlist("builtin"),
        Playlist(
            item_id="7",
            provider="library",
            name="Jazz",
            provider_mappings={_mapping("builtin", "pl1")},
        ),
    ],
    ids=["provider playlist", "library playlist"],
)
async def test_a_builtin_playlist_records_itself_but_pins_nothing(playlist: Playlist) -> None:
    """A Music Assistant playlist entry carries an arbitrary provider identity, so it is not pinned."""
    resolver = _resolver()
    track = _track("t1", TIDAL)
    resolver.get_playlist_tracks = AsyncMock(return_value=[track])  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(playlist)

    assert _container(resolved[0]) == ItemMapping.from_item(playlist)
    assert _pin(resolved[0]) == (None, None)


async def test_a_library_playlist_pins_the_instance_it_resolved_to() -> None:
    """A library playlist is listed by one of its instances, whose entries are pinned."""
    resolver = _resolver()
    playlist = Playlist(
        item_id="7",
        provider="library",
        name="Jazz",
        provider_mappings={_mapping(TIDAL, "pl1"), _mapping(SPOTIFY, "pl2")},
    )
    # the listing came from the Tidal instance
    track = _track("t1", TIDAL)
    resolver.get_playlist_tracks = AsyncMock(return_value=[track])  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(playlist)

    assert _container(resolved[0]).provider == "library"
    assert _pin(resolved[0]) == (TIDAL, "t1")


async def test_a_library_track_in_a_playlist_is_not_pinned() -> None:
    """A playlist entry that is a library track names no copy of it to play."""
    resolver = _resolver()
    track = _track("1", "library", _mapping(TIDAL, "t1"), _mapping(SPOTIFY, "t2"))
    resolver.get_playlist_tracks = AsyncMock(return_value=[track])  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(_playlist(TIDAL))

    assert _container(resolved[0]).media_type == MediaType.PLAYLIST
    assert _pin(resolved[0]) == (None, None)


async def test_an_album_play_records_the_album_without_a_pin() -> None:
    """An album is recorded as the container; which copy plays is not decided per track yet."""
    resolver = _resolver()
    album = Album(
        item_id="al1",
        provider=TIDAL,
        name="Kind of Blue",
        provider_mappings={_mapping(TIDAL, "al1")},
    )
    track = _track("t1", TIDAL)
    resolver.get_album_tracks = AsyncMock(return_value=[track])  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(album)

    assert resolved[0].item is track
    assert _container(resolved[0]) == ItemMapping.from_item(album)
    assert _container(resolved[0]).media_type == MediaType.ALBUM
    assert _pin(resolved[0]) == (None, None)


async def test_an_artist_play_records_the_artist_without_a_pin() -> None:
    """An artist is recorded as the container, but its tracks are not pinned to any copy."""
    resolver = _resolver()
    artist = Artist(
        item_id="ar1",
        provider=TIDAL,
        name="Miles Davis",
        provider_mappings={_mapping(TIDAL, "ar1")},
    )
    resolver.get_artist_tracks = AsyncMock(return_value=[_track("t1", TIDAL)])  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(artist)

    assert _container(resolved[0]).media_type == MediaType.ARTIST
    assert _pin(resolved[0]) == (None, None)


async def test_a_podcast_pins_its_episodes_to_the_podcasts_provider() -> None:
    """Episodes play from the provider the podcast was started from, so progress syncs there."""
    resolver = _resolver()
    podcast = Podcast(
        item_id="show", provider=GPODDER, name="Show", provider_mappings={_mapping(GPODDER, "show")}
    )
    episode = PodcastEpisode(
        item_id="ep1",
        provider=GPODDER,
        name="Episode 1",
        provider_mappings={_mapping(GPODDER, "ep1")},
        position=1,
        podcast=podcast,
    )
    resolver.get_next_podcast_episodes = AsyncMock(return_value=UniqueList([episode]))  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(podcast)

    assert _container(resolved[0]) == ItemMapping.from_item(podcast)
    assert _pin(resolved[0]) == (GPODDER, "ep1")


@pytest.mark.parametrize(
    "item",
    [_track("1", "library", _mapping(TIDAL, "t1")), _track("t1", TIDAL)],
    ids=["library track", "provider track"],
)
async def test_a_single_item_has_no_origin(item: MediaItemType) -> None:
    """An item played on its own was not played from anywhere."""
    resolver = _resolver()

    assert await resolver._resolve_media_items(item) == [ResolvedItem(item)]


async def test_a_folder_pins_its_entries_to_their_files() -> None:
    """A folder plays the files it lists, also for a library track that has copies elsewhere."""
    resolver = _resolver()
    folder = BrowseFolder(
        item_id=FOLDER_PATH, provider=FILESYSTEM, name="Kind of Blue", is_playable=True
    )
    listed = ItemMapping(
        media_type=MediaType.TRACK,
        item_id=f"{FOLDER_PATH}/01.flac",
        provider=FILESYSTEM,
        name="01.flac",
    )
    # the listed file is in the library, which also holds a streaming copy of it
    in_library = _track(
        "1", "library", _mapping(FILESYSTEM, f"{FOLDER_PATH}/01.flac"), _mapping(TIDAL, "t1")
    )
    provider_track = _track(f"{FOLDER_PATH}/02.flac", FILESYSTEM)
    two_copies = _track(
        "2",
        "library",
        _mapping(FILESYSTEM, "Compilations/03.flac"),
        _mapping(FILESYSTEM, f"{FOLDER_PATH}/03.flac"),
    )
    copies_elsewhere = _track(
        "3", "library", _mapping(FILESYSTEM, "Other/b.flac"), _mapping(FILESYSTEM, "Other/a.flac")
    )
    resolver.mass.music.browse = AsyncMock(  # type: ignore[method-assign]
        return_value=[listed, provider_track, two_copies, copies_elsewhere]
    )
    resolver.mass.music.get_item_by_uri = AsyncMock(return_value=in_library)  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(folder)

    assert [x.item for x in resolved] == [in_library, provider_track, two_copies, copies_elsewhere]
    for entry in resolved:
        container = _container(entry)
        assert (container.media_type, container.item_id, container.provider, container.name) == (
            MediaType.FOLDER,
            FOLDER_PATH,
            FILESYSTEM,
            "Kind of Blue",
        )
    assert [_pin(x) for x in resolved] == [
        # the file as listed, not the library track it resolved to
        (FILESYSTEM, f"{FOLDER_PATH}/01.flac"),
        (FILESYSTEM, f"{FOLDER_PATH}/02.flac"),
        # the copy under the folder
        (FILESYSTEM, f"{FOLDER_PATH}/03.flac"),
        # the first copy on the folder's provider
        (FILESYSTEM, "Other/a.flac"),
    ]


async def test_a_subfolder_and_a_playlist_in_a_folder_are_the_origin_of_their_own_entries() -> None:
    """What a subfolder or a playlist in the folder lists is played from that subfolder or playlist."""
    resolver = _resolver()
    folder = BrowseFolder(item_id="Music", provider=FILESYSTEM, name="Music", is_playable=True)
    subfolder = BrowseFolder(
        item_id="Music/Album", provider=FILESYSTEM, name="Album", is_playable=True
    )
    subfolder_track = _track("Music/Album/01.flac", FILESYSTEM)
    m3u = ItemMapping(
        media_type=MediaType.PLAYLIST, item_id="Music/mix.m3u", provider=FILESYSTEM, name="mix.m3u"
    )
    playlist = Playlist(
        item_id="Music/mix.m3u",
        provider=FILESYSTEM,
        name="mix",
        provider_mappings={_mapping(FILESYSTEM, "Music/mix.m3u")},
    )
    playlist_track = _track("Music/Other/02.flac", FILESYSTEM)
    resolver.mass.music.browse = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda path: [subfolder, m3u] if path == folder.path else [subfolder_track]
    )
    resolver.mass.music.get_item_by_uri = AsyncMock(return_value=playlist)  # type: ignore[method-assign]
    resolver.get_playlist_tracks = AsyncMock(return_value=[playlist_track])  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(folder)

    assert [x.item for x in resolved] == [subfolder_track, playlist_track]
    assert [(_container(x).media_type, _container(x).item_id) for x in resolved] == [
        (MediaType.FOLDER, "Music/Album"),
        (MediaType.PLAYLIST, "Music/mix.m3u"),
    ]
    assert [_pin(x) for x in resolved] == [
        (FILESYSTEM, "Music/Album/01.flac"),
        (FILESYSTEM, "Music/Other/02.flac"),
    ]
