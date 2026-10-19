"""
Tests for the origin a resolved item records: where it was played from, and which copy to play.

The resolver wraps every item a container resolves to with that container and, where the
listing had one specific copy of the item, with the (provider instance, item id) of that copy,
so playback can prefer the copy the user actually picked.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Collection
from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ContentType, ExternalID, MediaType, ProviderSharing
from music_assistant_models.helpers import get_global_cache_value, set_global_cache_values
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)
from music_assistant_models.queue_item import QueueItemOrigin

from music_assistant.controllers.music.media.album_tracks import (
    AlbumListing,
    album_listing,
    mapped_album_listing,
)
from music_assistant.controllers.player_queues.media_resolver import MediaResolver, ResolvedItem
from music_assistant.controllers.streams.constants import (
    CONF_STREAM_SOURCE_MODE,
    CONF_UPGRADE_BELOW_QUALITY,
)
from music_assistant.controllers.streams.stream_sources import (
    StreamSourceMode,
    UpgradeQuality,
    select_container_source,
)
from tests.common import set_music_source_access

TIDAL = "tidal--abc"
OTHER_TIDAL = "tidal--stu"
SPOTIFY = "spotify--def"
OWN_SPOTIFY = "spotify--vwx"
QOBUZ = "qobuz--mno"
GPODDER = "gpodder--ghi"
AI_RADIO = "ai_radio--jkl"
FILESYSTEM = "filesystem_local--xyz"
OTHER_FILESYSTEM = "filesystem_local--pqr"
FOLDER_PATH = "Music/Miles Davis/Kind of Blue"
CD_QUALITY = AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16)
HIRES = AudioFormat(content_type=ContentType.FLAC, sample_rate=96000, bit_depth=24)
LOSSY = AudioFormat(content_type=ContentType.OGG, bit_rate=320)
# the music sources an album plays from, each a household source unless a test says otherwise
ALBUM_SOURCES = (TIDAL, OTHER_TIDAL, SPOTIFY, OWN_SPOTIFY, QOBUZ, FILESYSTEM, OTHER_FILESYSTEM)
MEMBER = "member"
OWNED = ProviderAccess(owner=MEMBER, sharing=ProviderSharing.PRIVATE)


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


@pytest.fixture(name="loaded_sources")
async def loaded_sources_fixture() -> AsyncGenerator[None]:
    """Load the album's sources, so its tracks are available to play."""
    previous = get_global_cache_value("available_providers")
    await set_global_cache_values({"available_providers": set(ALBUM_SOURCES)})
    yield
    await set_global_cache_values({"available_providers": previous})


def _copy(
    instance: str, item_id: str, audio_format: AudioFormat = CD_QUALITY, available: bool = True
) -> ProviderMapping:
    """Build a copy of a track on the given provider instance, in the given format."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance.split("--", maxsplit=1)[0],
        provider_instance=instance,
        audio_format=audio_format,
        available=available,
    )


def _album_resolver(
    listing: AlbumListing,
    local: Collection[str] = (),
    gone: dict[str, str] | None = None,
    mode: StreamSourceMode = StreamSourceMode.BEST_QUALITY,
    upgrade: UpgradeQuality = UpgradeQuality.OFF,
) -> MediaResolver:
    """
    Build a bare resolver that plays an album the given listing lists.

    :param listing: The album's tracks and what each of its sources lists of them.
    :param local: The instances that are local sources rather than streaming services.
    :param gone: The instances that are no longer loaded, with the account answering for each.
    :param mode: The source selection mode of the streams settings.
    :param upgrade: The upgrade quality of the streams settings.
    """
    resolver = _resolver()
    # the album selection setting is not the library tracks only
    resolver.queues = Mock()
    resolver.mass.music.albums.playback_listing = AsyncMock(return_value=listing)  # type: ignore[method-assign]
    resolver.mass.webserver.auth.get_user = AsyncMock(return_value=None)  # type: ignore[method-assign]
    resolver.mass.streams.get_config_value = Mock(  # type: ignore[method-assign]
        side_effect=lambda key, *_args, **_kwargs: {
            CONF_STREAM_SOURCE_MODE: mode,
            CONF_UPGRADE_BELOW_QUALITY: upgrade,
        }[key]
    )
    set_music_source_access(resolver.mass, dict.fromkeys(ALBUM_SOURCES))

    def _get_provider(instance: str, **_kwargs: Any) -> SimpleNamespace:
        instance = (gone or {}).get(instance, instance)
        return SimpleNamespace(
            instance_id=instance,
            domain=instance.split("--", maxsplit=1)[0],
            is_streaming_provider=instance not in local,
            available=True,
        )

    resolver.mass.get_provider = Mock(side_effect=_get_provider)  # type: ignore[method-assign]
    return resolver


def _sources(
    allowed: list[str] | None = None, preferred: Collection[str] = ()
) -> AbstractContextManager[Mock]:
    """Return the (allowed, preferred) music sources of the playback user, while in use."""
    return patch(
        f"{MediaResolver.__module__}.playback_sources_for",
        return_value=(allowed, list(preferred)),
    )


def _play_for_member(resolver: MediaResolver, access: dict[str, ProviderAccess | None]) -> None:
    """
    Play for a member of the household, with the music sources given.

    :param resolver: The resolver that plays the album.
    :param access: The access record of each music source; None for a household source.
    """
    set_music_source_access(resolver.mass, access)
    resolver.mass.webserver.auth.get_user = AsyncMock(  # type: ignore[method-assign]
        return_value=User(user_id=MEMBER, username=MEMBER, role=UserRole.USER)
    )


def _album(provider: str = "library") -> Album:
    """Build the album being played: a library album, or one of a provider."""
    return Album(
        item_id="7" if provider == "library" else "al1",
        provider=provider,
        name="Kind of Blue",
        provider_mappings={_mapping(TIDAL, "al1"), _mapping(FILESYSTEM, FOLDER_PATH)},
    )


def _two_sources_and_a_bonus() -> AlbumListing:
    """
    Return an album whose two tracks are on the files and on Tidal, in hi-res, plus a bonus.

    The bonus track is in nobody's library and only Qobuz lists it.
    """
    first = _track(
        "1", "library", _copy(FILESYSTEM, f"{FOLDER_PATH}/01.flac"), _copy(TIDAL, "t1", HIRES)
    )
    second = _track(
        "2", "library", _copy(FILESYSTEM, f"{FOLDER_PATH}/02.flac"), _copy(TIDAL, "t2", HIRES)
    )
    bonus = _track("q3", QOBUZ, _copy(QOBUZ, "q3"))
    return AlbumListing(
        [first, second, bonus],
        {
            FILESYSTEM: {
                str(first.uri): f"{FOLDER_PATH}/01.flac",
                str(second.uri): f"{FOLDER_PATH}/02.flac",
            },
            TIDAL: {str(first.uri): "t1", str(second.uri): "t2"},
            QOBUZ: {str(bonus.uri): "q3"},
        },
    )


@pytest.mark.usefixtures("loaded_sources")
async def test_an_album_play_pins_every_track_to_the_one_source_chosen() -> None:
    """The album plays from its best complete source; a track that source lacks keeps its entry."""
    listing = _two_sources_and_a_bonus()
    resolver = _album_resolver(listing, local={FILESYSTEM})
    album = _album()

    with _sources():
        resolved = await resolver._resolve_media_items(album)

    assert [x.item for x in resolved] == listing.tracks
    assert [_container(x) for x in resolved] == [ItemMapping.from_item(album)] * 3
    assert _container(resolved[0]).media_type == MediaType.ALBUM
    assert [_pin(x) for x in resolved] == [(TIDAL, "t1"), (TIDAL, "t2"), (QOBUZ, "q3")]
    debug = cast("Mock", resolver.logger).debug
    assert any(call.args[1:] == ("q3", TIDAL, album.name) for call in debug.call_args_list)


@pytest.mark.usefixtures("loaded_sources")
@pytest.mark.parametrize(
    ("allowed", "preferred", "pins"),
    [
        (
            [FILESYSTEM, QOBUZ],
            [],
            [(FILESYSTEM, f"{FOLDER_PATH}/01.flac"), (FILESYSTEM, f"{FOLDER_PATH}/02.flac")],
        ),
        (None, [QOBUZ], [(None, None), (None, None), (QOBUZ, "q3")]),
    ],
    ids=["a source the user may not use is passed over", "the user's own account comes first"],
)
async def test_an_album_plays_from_a_source_of_the_playback_user(
    allowed: list[str] | None,
    preferred: list[str],
    pins: list[tuple[str | None, str | None]],
) -> None:
    """The album source is chosen among the sources of the user the album is played for."""
    resolver = _album_resolver(_two_sources_and_a_bonus(), local={FILESYSTEM})
    user = Mock()
    get_user = AsyncMock(return_value=user)
    resolver.mass.webserver.auth.get_user = get_user  # type: ignore[method-assign]

    with _sources(allowed, preferred) as sources:
        resolved = await resolver._resolve_media_items(_album(), userid="u1", queue_id="q1")

    get_user.assert_awaited_once_with("u1")
    sources.assert_called_once_with(resolver.mass, user)
    assert [_pin(x) for x in resolved][: len(pins)] == pins


@pytest.mark.usefixtures("loaded_sources")
@pytest.mark.parametrize(
    ("mode", "pins"),
    [
        (StreamSourceMode.BEST_QUALITY, [(TIDAL, "t1"), (TIDAL, "t2"), (QOBUZ, "q3")]),
        (
            StreamSourceMode.PREFER_LOCAL,
            [
                (FILESYSTEM, f"{FOLDER_PATH}/01.flac"),
                (FILESYSTEM, f"{FOLDER_PATH}/02.flac"),
                (QOBUZ, "q3"),
            ],
        ),
        (StreamSourceMode.BEST_QUALITY_PER_TRACK, [(None, None)] * 3),
    ],
    ids=["best quality", "prefer local", "best quality for every track"],
)
async def test_the_source_selection_mode_decides_the_album_source(
    mode: StreamSourceMode, pins: list[tuple[str | None, str | None]]
) -> None:
    """Preferring local plays the album from the files; ranking every track on its own pins none."""
    resolver = _album_resolver(_two_sources_and_a_bonus(), local={FILESYSTEM}, mode=mode)
    album = _album()

    with _sources():
        resolved = await resolver._resolve_media_items(album, queue_id="q1")

    assert [_container(x) for x in resolved] == [ItemMapping.from_item(album)] * 3
    assert [_pin(x) for x in resolved] == pins


def _entry(instance: str, position: int, audio_format: AudioFormat) -> Track:
    """Build the entry of a streaming service's listing of the album at the given position."""
    track = _track(
        f"{instance[0]}{position}",
        instance,
        _copy(instance, f"{instance[0]}{position}", audio_format),
    )
    track.external_ids = {(ExternalID.ISRC, f"GBAYC21{position:05d}")}
    track.disc_number, track.track_number = 1, position
    return track


@pytest.mark.usefixtures("loaded_sources")
async def test_an_album_of_provider_entries_plays_the_chosen_sources_own_entries() -> None:
    """Tracks that are not in the library play as the chosen service's entries, where it has them."""
    spotify = [_entry(SPOTIFY, position, LOSSY) for position in (1, 2, 3)]
    tidal = [_entry(TIDAL, position, HIRES) for position in (1, 2)]
    # the album page lists Spotify's entries, the first service by instance id
    listing = album_listing([], [(TIDAL, tidal), (SPOTIFY, spotify)])
    assert listing.tracks == spotify
    resolver = _album_resolver(listing)
    album = _album()

    with _sources():
        resolved = await resolver._resolve_media_items(album)

    assert [x.item for x in resolved] == [*tidal, spotify[2]]
    assert [_container(x) for x in resolved] == [ItemMapping.from_item(album)] * 3
    assert [_pin(x) for x in resolved] == [(TIDAL, "t1"), (TIDAL, "t2"), (SPOTIFY, "s3")]


@pytest.mark.usefixtures("loaded_sources")
@pytest.mark.parametrize(
    ("spotify_format", "tidal_format", "preferred"),
    [(LOSSY, HIRES, ()), (HIRES, LOSSY, [TIDAL])],
    ids=["the best quality", "the user's own account before a better quality"],
)
async def test_a_track_outside_the_library_plays_its_best_entry_when_ranked_on_its_own(
    spotify_format: AudioFormat, tidal_format: AudioFormat, preferred: list[str]
) -> None:
    """Ranking every track on its own, a track not in the library plays the best entry listed."""
    spotify = [_entry(SPOTIFY, position, spotify_format) for position in (1, 2, 3)]
    tidal = [_entry(TIDAL, position, tidal_format) for position in (1, 2)]
    # the album page lists Spotify's entries, the first service by instance id
    listing = album_listing([], [(TIDAL, tidal), (SPOTIFY, spotify)])
    resolver = _album_resolver(listing, mode=StreamSourceMode.BEST_QUALITY_PER_TRACK)
    album = _album()

    with _sources(preferred=preferred):
        resolved = await resolver._resolve_media_items(album)

    assert [x.item for x in resolved] == [*tidal, spotify[2]]
    assert [_container(x) for x in resolved] == [ItemMapping.from_item(album)] * 3
    assert [_pin(x) for x in resolved] == [(None, None)] * 3


@pytest.mark.usefixtures("loaded_sources")
async def test_a_track_outside_the_library_plays_an_entry_above_the_upgrade_quality() -> None:
    """Ranking every track on its own, a better entry goes before the own account's poorer one."""
    spotify = [_entry(SPOTIFY, position, HIRES) for position in (1, 2, 3)]
    tidal = [_entry(TIDAL, position, LOSSY) for position in (1, 2)]
    listing = album_listing([], [(TIDAL, tidal), (SPOTIFY, spotify)])
    resolver = _album_resolver(
        listing, mode=StreamSourceMode.BEST_QUALITY_PER_TRACK, upgrade=UpgradeQuality.LOSSLESS
    )

    with _sources(preferred=[TIDAL]):
        resolved = await resolver._resolve_media_items(_album())

    assert [x.item for x in resolved] == spotify
    assert [_pin(x) for x in resolved] == [(None, None)] * 3


@pytest.mark.usefixtures("loaded_sources")
async def test_an_album_outside_the_library_pins_its_tracks_to_their_own_copies() -> None:
    """An album as its provider lists it plays from that provider."""
    tracks = [_track("t1", TIDAL), _track("t2", TIDAL)]
    resolver = _album_resolver(mapped_album_listing(tracks))

    with _sources():
        resolved = await resolver._resolve_media_items(_album(TIDAL))

    cast("AsyncMock", resolver.mass.music.albums.playback_listing).assert_awaited_once_with(
        "al1", TIDAL, in_library_only=False, sort_field=None, sort_direction=None
    )
    assert [_pin(x) for x in resolved] == [(TIDAL, "t1"), (TIDAL, "t2")]


@pytest.mark.usefixtures("loaded_sources")
async def test_an_album_plays_only_its_library_tracks_when_set_to() -> None:
    """The album selection setting decides whether only the library tracks are listed."""
    resolver = _album_resolver(mapped_album_listing([_track("t1", TIDAL)]))
    resolver.mass.config.get_raw_core_config_value = Mock(return_value="library_tracks")  # type: ignore[method-assign]

    with _sources():
        await resolver._resolve_media_items(_album(TIDAL))

    cast("AsyncMock", resolver.mass.music.albums.playback_listing).assert_awaited_once_with(
        "al1", TIDAL, in_library_only=True, sort_field=None, sort_direction=None
    )


@pytest.mark.usefixtures("loaded_sources")
async def test_an_album_play_starts_at_the_chosen_track() -> None:
    """The album plays from the chosen track on, the tracks before it at the end when asked."""
    tracks = [_track("t1", TIDAL), _track("t2", TIDAL), _track("t3", TIDAL)]
    resolver = _album_resolver(mapped_album_listing(tracks))

    with _sources():
        resolved = await resolver._resolve_media_items(
            _album(TIDAL), start_item="t2", keep_preceding_items=True
        )

    assert [_pin(x) for x in resolved] == [(TIDAL, "t2"), (TIDAL, "t3"), (TIDAL, "t1")]


@pytest.mark.usefixtures("loaded_sources")
async def test_another_account_of_the_service_plays_the_copies_it_lists() -> None:
    """An account that listed the album in place of the mapped one plays the same item ids."""
    row = _track("1", "library", _copy(TIDAL, "t1", HIRES))
    resolver = _album_resolver(AlbumListing([row], {OTHER_TIDAL: {str(row.uri): "t1"}}))

    with _sources():
        resolved = await resolver._resolve_media_items(_album())

    assert _pin(resolved[0]) == (OTHER_TIDAL, "t1")


@pytest.mark.usefixtures("loaded_sources")
@pytest.mark.parametrize(
    ("row", "listed_ids", "gone"),
    [
        # another local library may hold a file under the same path, it is another file
        (
            _track("1", "library", _copy(FILESYSTEM, "01.flac")),
            {OTHER_FILESYSTEM: "01.flac"},
            None,
        ),
        (
            _track(
                "1", "library", _copy(TIDAL, "t1", available=False), _copy(FILESYSTEM, "01.flac")
            ),
            {TIDAL: "t1"},
            None,
        ),
        (
            _track("1", "library", _copy(TIDAL, "t1")),
            {TIDAL: "t1"},
            {TIDAL: OTHER_TIDAL},
        ),
        # the source no longer finds the id, which another account still plays
        (
            _track("1", "library", _copy(TIDAL, "t1", available=False), _copy(OTHER_TIDAL, "t1")),
            {TIDAL: "t1"},
            None,
        ),
    ],
    ids=[
        "another local library",
        "an unavailable copy",
        "a source no longer loaded",
        "an id the source no longer finds",
    ],
)
async def test_a_source_that_can_not_play_its_copy_is_no_album_source(
    row: Track, listed_ids: dict[str, str], gone: dict[str, str] | None
) -> None:
    """A source is chosen only for the copies it can actually play."""
    listing = AlbumListing(
        [row], {instance: {str(row.uri): item_id} for instance, item_id in listed_ids.items()}
    )
    resolver = _album_resolver(listing, local={FILESYSTEM, OTHER_FILESYSTEM}, gone=gone)

    with _sources():
        resolved = await resolver._resolve_media_items(_album())

    assert _container(resolved[0]) == ItemMapping.from_item(_album())
    assert _pin(resolved[0]) == (None, None)


def _household_spotify_and_tidal() -> AlbumListing:
    """Return an album whose two tracks are on the household's Spotify and, in hi-res, its Tidal."""
    rows = [
        _track(
            str(position),
            "library",
            _copy(SPOTIFY, f"s{position}", LOSSY),
            _copy(TIDAL, f"t{position}", HIRES),
        )
        for position in (1, 2)
    ]
    return AlbumListing(
        rows,
        {
            SPOTIFY: {str(row.uri): f"s{row.item_id}" for row in rows},
            TIDAL: {str(row.uri): f"t{row.item_id}" for row in rows},
        },
    )


@pytest.mark.usefixtures("loaded_sources")
@pytest.mark.parametrize(
    ("access", "pins"),
    [
        ({OWN_SPOTIFY: OWNED}, [(OWN_SPOTIFY, "s1"), (OWN_SPOTIFY, "s2")]),
        ({}, [(TIDAL, "t1"), (TIDAL, "t2")]),
    ],
    ids=["the member owns an account of the service", "the member owns no account"],
)
async def test_a_household_listing_plays_from_the_members_own_account_of_the_service(
    access: dict[str, ProviderAccess | None], pins: list[tuple[str | None, str | None]]
) -> None:
    """The member's own account plays what the household's account of its service lists."""
    resolver = _album_resolver(_household_spotify_and_tidal())
    _play_for_member(resolver, {SPOTIFY: None, TIDAL: None, **access})

    resolved = await resolver._resolve_media_items(_album(), userid=MEMBER)

    assert [_pin(x) for x in resolved] == pins


@pytest.mark.usefixtures("loaded_sources")
async def test_a_household_listing_and_the_members_own_listing_make_one_source() -> None:
    """The member's own listing counts first; the household's listing fills in what it lacks."""
    first = _track(
        "1",
        "library",
        _copy(SPOTIFY, "s1", LOSSY),
        _copy(OWN_SPOTIFY, "o1", LOSSY),
        _copy(TIDAL, "t1", HIRES),
    )
    second = _track("2", "library", _copy(SPOTIFY, "s2", LOSSY), _copy(TIDAL, "t2", HIRES))
    listing = AlbumListing(
        [first, second],
        {
            SPOTIFY: {str(first.uri): "s1", str(second.uri): "s2"},
            OWN_SPOTIFY: {str(first.uri): "o1"},
            TIDAL: {str(first.uri): "t1", str(second.uri): "t2"},
        },
    )
    resolver = _album_resolver(listing)
    _play_for_member(resolver, {SPOTIFY: None, TIDAL: None, OWN_SPOTIFY: OWNED})

    with patch(
        f"{MediaResolver.__module__}.select_container_source", wraps=select_container_source
    ) as select:
        resolved = await resolver._resolve_media_items(_album(), userid=MEMBER)

    listed = select.call_args.args[0]
    assert sorted(source.instance_id for source in listed) == [OWN_SPOTIFY, TIDAL]
    assert [_pin(x) for x in resolved] == [(OWN_SPOTIFY, "o1"), (OWN_SPOTIFY, "s2")]


@pytest.mark.usefixtures("loaded_sources")
async def test_a_local_source_the_member_may_not_use_is_not_played_from_their_own() -> None:
    """Another library is no stand-in for a local source, even with a file under the same path."""
    row = _track(
        "1",
        "library",
        _copy(FILESYSTEM, "01.flac"),
        _copy(OTHER_FILESYSTEM, "01.flac"),
        _copy(TIDAL, "t1", LOSSY),
    )
    listing = AlbumListing(
        [row], {FILESYSTEM: {str(row.uri): "01.flac"}, TIDAL: {str(row.uri): "t1"}}
    )
    resolver = _album_resolver(listing, local={FILESYSTEM, OTHER_FILESYSTEM})
    _play_for_member(
        resolver,
        {
            FILESYSTEM: ProviderAccess(owner="someone else", sharing=ProviderSharing.PRIVATE),
            OTHER_FILESYSTEM: OWNED,
            TIDAL: None,
        },
    )

    resolved = await resolver._resolve_media_items(_album(), userid=MEMBER)

    assert _pin(resolved[0]) == (TIDAL, "t1")


@pytest.mark.usefixtures("loaded_sources")
async def test_a_track_ranked_on_its_own_plays_from_the_members_own_account() -> None:
    """Ranking every track on its own, the member's own account plays the household's entries."""
    spotify = [_entry(SPOTIFY, position, LOSSY) for position in (1, 2, 3)]
    tidal = [_entry(TIDAL, position, HIRES) for position in (1, 2)]
    listing = album_listing([], [(TIDAL, tidal), (SPOTIFY, spotify)])
    resolver = _album_resolver(listing, mode=StreamSourceMode.BEST_QUALITY_PER_TRACK)
    _play_for_member(resolver, {SPOTIFY: None, TIDAL: None, OWN_SPOTIFY: OWNED})

    resolved = await resolver._resolve_media_items(_album(), userid=MEMBER)

    assert [x.item for x in resolved] == spotify
    assert [_pin(x) for x in resolved] == [(None, None)] * 3


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


def _episode(provider: str) -> PodcastEpisode:
    """Build an episode of a gPodder podcast, as the given provider lists it."""
    podcast = Podcast(
        item_id="show", provider=GPODDER, name="Show", provider_mappings={_mapping(GPODDER, "show")}
    )
    return PodcastEpisode(
        item_id="ep1",
        provider=provider,
        name="Episode 1",
        provider_mappings={_mapping(GPODDER, "ep1")},
        position=1,
        podcast=podcast,
    )


@pytest.mark.parametrize(
    ("provider", "origin"),
    [
        (GPODDER, QueueItemOrigin(provider_instance=GPODDER, item_id="ep1")),
        ("library", None),
    ],
    ids=["provider episode", "library episode"],
)
async def test_an_episode_played_on_its_own_plays_from_its_provider(
    provider: str, origin: QueueItemOrigin | None
) -> None:
    """An episode played on its own keeps the provider copy it was started from."""
    resolver = _resolver()
    episode = _episode(provider)
    resolver.get_next_podcast_episodes = AsyncMock(return_value=UniqueList([episode]))  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(episode)

    assert resolved == [ResolvedItem(episode, origin)]


async def test_an_episode_in_a_folder_is_played_from_the_folder() -> None:
    """An episode the folder lists records the folder, pinned to the episode's own copy."""
    resolver = _resolver()
    folder = BrowseFolder(item_id="Up next", provider=GPODDER, name="Up next", is_playable=True)
    episode = _episode(GPODDER)
    resolver.mass.music.browse = AsyncMock(return_value=[episode])  # type: ignore[method-assign]
    resolver.get_next_podcast_episodes = AsyncMock(return_value=UniqueList([episode]))  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(folder)

    assert (_container(resolved[0]).media_type, _container(resolved[0]).item_id) == (
        MediaType.FOLDER,
        "Up next",
    )
    assert _pin(resolved[0]) == (GPODDER, "ep1")


async def test_a_finite_radio_station_records_itself_but_pins_nothing() -> None:
    """A station's tracklist can come from a builtin playlist, so its entries are not pinned."""
    resolver = _resolver()
    show = Radio(
        item_id="show",
        provider=AI_RADIO,
        name="Morning show",
        provider_mappings={_mapping(AI_RADIO, "show")},
        is_dynamic=False,
        is_endless=False,
    )
    track = _track("t1", TIDAL)
    resolver.mass.music.radio.tracks = AsyncMock(return_value=[track])  # type: ignore[method-assign]

    resolved = await resolver._resolve_media_items(show)

    assert [x.item for x in resolved] == [track]
    assert _container(resolved[0]) == ItemMapping.from_item(show)
    assert _pin(resolved[0]) == (None, None)


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
        side_effect=lambda path, **_kwargs: (
            [subfolder, m3u] if path == folder.path else [subfolder_track]
        )
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
