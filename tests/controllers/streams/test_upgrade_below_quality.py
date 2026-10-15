"""
Tests for upgrading a copy below the upgrade quality when its stream is selected.

When the copy a selection would play is below the upgrade quality of the streams settings, a
better copy the track has plays instead. A library track without one is searched for on the
other streaming services once per queue item, and what is found is stored without merging
library items. A service that found no match is not searched again for a while.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    ProviderFeature,
    ProviderSharing,
    ProviderType,
    StreamType,
)
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    ItemMapping,
    PodcastEpisode,
    ProviderMapping,
    Track,
)
from music_assistant_models.queue_item import QueueItem, QueueItemOrigin
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.constants import (
    CONF_UPGRADE_BELOW_QUALITY,
    NO_TRACK_MATCH_EXPIRATION,
)
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access

LOCAL = "filesystem_local--files"
SPOTIFY = "spotify--ogg"
TIDAL = "tidal--hifi"
# a service whose catalog gives no format
YANDEX = "yandex_music--own"
LIBRARY_ID = "42"
LOCAL_ID = "Artist/Album/01.mp3"
SPOTIFY_ID = "spotify-1"
TIDAL_ID = "tidal-1"
USER_ID = "listener"

MP3 = AudioFormat(content_type=ContentType.MP3, bit_rate=320)
OGG = AudioFormat(content_type=ContentType.OGG, bit_rate=320)
FLAC = AudioFormat(content_type=ContentType.FLAC)


def _mapping(instance: str, item_id: str, audio_format: AudioFormat) -> ProviderMapping:
    """Build the mapping of a copy on the given provider instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance.split("--", maxsplit=1)[0],
        provider_instance=instance,
        audio_format=audio_format,
    )


def _provider(instance: str, *, streaming: bool = True) -> MagicMock:
    """Build a loaded music provider that streams whatever it is asked for."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance
    provider.domain = instance.split("--", maxsplit=1)[0]
    provider.name = provider.domain
    provider.type = ProviderType.MUSIC
    provider.available = True
    provider.is_streaming_provider = streaming
    provider.has_available_stream_slot = True
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}

    async def _stream_details(item_id: str, media_type: MediaType) -> StreamDetails:
        return StreamDetails(
            provider=instance,
            item_id=item_id,
            audio_format=AudioFormat(),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path="http://test.invalid/track",
            duration=30,
        )

    provider.get_stream_details = AsyncMock(side_effect=_stream_details)
    return provider


def _mass(
    providers: list[MagicMock], upgrade: str = "lossless", owned: tuple[str, ...] = ()
) -> MagicMock:
    """
    Build a mass double whose sources may all serve the playback.

    :param providers: The loaded provider instances.
    :param upgrade: The upgrade quality of the streams settings.
    :param owned: The sources the playback user owns, the others being household sources.
        Leave empty for a queue without a playback user.
    """
    by_instance = {provider.instance_id: provider for provider in providers}
    mass = MagicMock()
    mass.providers = providers
    mass.get_provider.side_effect = lambda instance, **_kwargs: by_instance.get(instance)
    mass.player_queues.queue_data_or_none.return_value = None
    if owned:
        mass.player_queues.queue_data_or_none.return_value = MagicMock(userid=USER_ID)
        mass.webserver.auth.get_user = AsyncMock(
            return_value=User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
        )
        set_music_source_access(
            mass,
            {
                provider.instance_id: ProviderAccess(owner=USER_ID, sharing=ProviderSharing.PRIVATE)
                if provider.instance_id in owned
                else None
                for provider in providers
            },
        )
    mass.streams.get_config_value.side_effect = lambda key, *_args, **_kwargs: (
        upgrade if key == CONF_UPGRADE_BELOW_QUALITY else -17
    )
    mass.music.providers = providers
    mass.music.tracks.match_provider = AsyncMock(return_value=[])
    mass.music.tracks.add_unclaimed_provider_mappings = AsyncMock()
    cache: dict[tuple[str, int, str], Any] = {}

    async def _cache_get(key: str, provider: str, category: int, **_kwargs: Any) -> Any:
        return cache.get((provider, category, key))

    async def _cache_set(key: str, data: Any, provider: str, category: int, **_kwargs: Any) -> None:
        cache[(provider, category, key)] = data

    mass.cache.get = AsyncMock(side_effect=_cache_get)
    mass.cache.set = AsyncMock(side_effect=_cache_set)

    def _schedule(coro: Any, *_args: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return task

    mass.create_task.side_effect = _schedule
    return mass


def _queue_item(
    *mappings: ProviderMapping,
    provider: str = "library",
    queue_item_id: str = "qi1",
    pinned: ProviderMapping | None = None,
) -> QueueItem:
    """
    Build a queue item of a track with the given copies.

    :param mappings: The copies the track has.
    :param provider: The track's provider; a library track by default.
    :param queue_item_id: The id of the queue item.
    :param pinned: The copy the album the track was played from listed for it.
    """
    track = Track(
        item_id=LIBRARY_ID if provider == "library" else mappings[0].item_id,
        provider=provider,
        name="Song",
        provider_mappings=set(mappings),
    )
    origin = (
        QueueItemOrigin(
            container=ItemMapping(
                media_type=MediaType.ALBUM, item_id="7", provider="library", name="Album"
            ),
            provider_instance=pinned.provider_instance,
            item_id=pinned.item_id,
        )
        if pinned
        else None
    )
    return QueueItem(
        queue_id="q1",
        queue_item_id=queue_item_id,
        name="Song",
        duration=30,
        media_item=track,
        origin=origin,
    )


async def test_a_copy_below_the_upgrade_quality_plays_a_better_match() -> None:
    """A pinned MP3 below lossless plays the lossless copy found on another service."""
    local = _provider(LOCAL, streaming=False)
    tidal = _provider(TIDAL)
    mass = _mass([local, tidal])
    match = _mapping(TIDAL, TIDAL_ID, FLAC)
    mass.music.tracks.match_provider = AsyncMock(return_value=[match])
    audio = StreamsAudio(mass)
    mp3 = _mapping(LOCAL, LOCAL_ID, MP3)

    streamdetails = await audio.get_stream_details(_queue_item(mp3, pinned=mp3))

    assert (streamdetails.provider, streamdetails.item_id) == (TIDAL, TIDAL_ID)
    local.get_stream_details.assert_not_awaited()
    # strictness is what prevents ever playing the wrong track
    assert mass.music.tracks.match_provider.await_args.args[1] is tidal
    assert mass.music.tracks.match_provider.await_args.kwargs["strict"] is True
    # stored for the next plays, without merging another library item into this one
    await asyncio.sleep(0)
    mass.music.tracks.add_unclaimed_provider_mappings.assert_awaited_once_with(LIBRARY_ID, [match])


async def test_the_search_runs_once_per_queue_item() -> None:
    """A copy the search does not find stays as chosen, and the item is not searched again."""
    local = _provider(LOCAL, streaming=False)
    mass = _mass([local, _provider(TIDAL)])
    # a failed search is no miss to remember, so only the item's own search holds it back
    mass.music.tracks.match_provider = AsyncMock(side_effect=KeyError("unexpected response"))
    audio = StreamsAudio(mass)
    queue_item = _queue_item(_mapping(LOCAL, LOCAL_ID, MP3))

    first = await audio.get_stream_details(queue_item)
    # a later selection of the same item, such as after its details expired
    queue_item.streamdetails = None
    second = await audio.get_stream_details(queue_item)

    assert first.provider == second.provider == LOCAL
    mass.music.tracks.match_provider.assert_awaited_once()


async def test_a_search_cut_short_runs_again() -> None:
    """A search that was cancelled, such as by a skip, is not the item's one search."""
    local = _provider(LOCAL, streaming=False)
    mass = _mass([local, _provider(TIDAL)])
    searching = asyncio.Event()

    async def _never_done(*_args: Any, **_kwargs: Any) -> list[ProviderMapping]:
        searching.set()
        await asyncio.Event().wait()
        return []

    mass.music.tracks.match_provider = AsyncMock(side_effect=_never_done)
    audio = StreamsAudio(mass)
    queue_item = _queue_item(_mapping(LOCAL, LOCAL_ID, MP3))
    selection = asyncio.create_task(audio.get_stream_details(queue_item))
    await searching.wait()
    selection.cancel()
    with pytest.raises(asyncio.CancelledError):
        await selection

    mass.music.tracks.match_provider = AsyncMock(return_value=[])
    streamdetails = await audio.get_stream_details(queue_item)

    assert streamdetails.provider == LOCAL
    mass.music.tracks.match_provider.assert_awaited_once()


async def test_a_service_without_a_match_is_not_searched_again_for_a_while() -> None:
    """A service that found no match for the track is skipped by the next plays for a day."""
    mass = _mass([_provider(LOCAL, streaming=False), _provider(TIDAL)])
    audio = StreamsAudio(mass)

    await audio.get_stream_details(_queue_item(_mapping(LOCAL, LOCAL_ID, MP3)))
    # the miss is stored in the background
    await asyncio.sleep(0)
    streamdetails = await audio.get_stream_details(
        _queue_item(_mapping(LOCAL, LOCAL_ID, MP3), queue_item_id="qi2")
    )

    assert streamdetails.provider == LOCAL
    mass.music.tracks.match_provider.assert_awaited_once()
    assert mass.cache.set.await_args.kwargs["expiration"] == NO_TRACK_MATCH_EXPIRATION


async def test_a_service_whose_search_failed_is_searched_again() -> None:
    """A search that failed says nothing about the track, so the next play searches again."""
    mass = _mass([_provider(LOCAL, streaming=False), _provider(TIDAL)])
    mass.music.tracks.match_provider = AsyncMock(side_effect=KeyError("unexpected response"))
    audio = StreamsAudio(mass)

    await audio.get_stream_details(_queue_item(_mapping(LOCAL, LOCAL_ID, MP3)))
    await asyncio.sleep(0)
    streamdetails = await audio.get_stream_details(
        _queue_item(_mapping(LOCAL, LOCAL_ID, MP3), queue_item_id="qi2")
    )

    assert streamdetails.provider == LOCAL
    assert mass.music.tracks.match_provider.await_count == 2
    mass.cache.set.assert_not_awaited()


async def test_the_search_has_one_time_budget_for_all_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service that uses up the time budget ends the search, and is not taken as a miss."""
    monkeypatch.setattr("music_assistant.controllers.streams.audio.STREAM_SLOT_MATCH_TIMEOUT", 0.05)
    mass = _mass([_provider(LOCAL, streaming=False), _provider(SPOTIFY), _provider(TIDAL)])

    async def _never_done(*_args: Any, **_kwargs: Any) -> list[ProviderMapping]:
        await asyncio.Event().wait()
        return []

    mass.music.tracks.match_provider = AsyncMock(side_effect=_never_done)
    audio = StreamsAudio(mass)

    streamdetails = await audio.get_stream_details(_queue_item(_mapping(LOCAL, LOCAL_ID, MP3)))

    assert streamdetails.provider == LOCAL
    mass.music.tracks.match_provider.assert_awaited_once()
    await asyncio.sleep(0)
    mass.cache.set.assert_not_awaited()


async def test_concurrent_selections_of_one_item_search_once() -> None:
    """Two callers resolving the same queue item at once share its one search."""
    mass = _mass([_provider(LOCAL, streaming=False), _provider(TIDAL)])
    release = asyncio.Event()

    async def _slow(*_args: Any, **_kwargs: Any) -> list[ProviderMapping]:
        await release.wait()
        return []

    mass.music.tracks.match_provider = AsyncMock(side_effect=_slow)
    audio = StreamsAudio(mass)
    queue_item = _queue_item(_mapping(LOCAL, LOCAL_ID, MP3))

    first = asyncio.create_task(audio.get_stream_details(queue_item))
    second = asyncio.create_task(audio.get_stream_details(queue_item))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    mass.music.tracks.match_provider.assert_awaited_once()


async def test_a_reselection_around_a_stream_limit_does_not_search() -> None:
    """A copy picked because a better one is at its stream limit is no reason to search."""
    local = _provider(LOCAL, streaming=False)
    mass = _mass([local, _provider(TIDAL), _provider(SPOTIFY)])
    audio = StreamsAudio(mass)
    queue_item = _queue_item(_mapping(LOCAL, LOCAL_ID, MP3), _mapping(TIDAL, TIDAL_ID, FLAC))

    streamdetails = await audio.get_stream_details(queue_item, excluded_provider_instances={TIDAL})

    assert streamdetails.provider == LOCAL
    mass.music.tracks.match_provider.assert_not_awaited()


async def test_a_copy_of_unknown_quality_is_not_upgraded() -> None:
    """A copy whose format is not known yet, such as a linked one, plays and is not upgraded."""
    tidal = _provider(TIDAL)
    mass = _mass([_provider(LOCAL, streaming=False), tidal, _provider(SPOTIFY)])
    audio = StreamsAudio(mass)
    linked = _mapping(TIDAL, TIDAL_ID, AudioFormat())

    streamdetails = await audio.get_stream_details(
        _queue_item(
            linked,
            _mapping(LOCAL, LOCAL_ID, AudioFormat(content_type=ContentType.MP3, bit_rate=128)),
            pinned=linked,
        )
    )

    assert streamdetails.provider == TIDAL
    mass.music.tracks.match_provider.assert_not_awaited()


@pytest.mark.parametrize(
    ("upgrade", "audio_format"),
    [("off", MP3), ("lossy_high", MP3), ("lossless", FLAC)],
    ids=["upgrade off", "mp3 meets high-bitrate lossy", "flac meets lossless"],
)
async def test_no_search_when_the_chosen_copy_is_not_below_the_upgrade_quality(
    upgrade: str, audio_format: AudioFormat
) -> None:
    """Nothing is searched with the setting off or for a copy that meets the upgrade quality."""
    mass = _mass([_provider(LOCAL, streaming=False), _provider(TIDAL)], upgrade=upgrade)
    audio = StreamsAudio(mass)

    streamdetails = await audio.get_stream_details(
        _queue_item(_mapping(LOCAL, LOCAL_ID, audio_format))
    )

    assert streamdetails.provider == LOCAL
    mass.music.tracks.match_provider.assert_not_awaited()


async def test_a_track_outside_the_library_is_not_searched() -> None:
    """Only a library track is searched for, as only the library keeps what is found."""
    mass = _mass([_provider(LOCAL, streaming=False), _provider(TIDAL)])
    audio = StreamsAudio(mass)

    streamdetails = await audio.get_stream_details(
        _queue_item(_mapping(LOCAL, LOCAL_ID, MP3), provider=LOCAL)
    )

    assert streamdetails.provider == LOCAL
    mass.music.tracks.match_provider.assert_not_awaited()


async def test_a_better_copy_the_track_has_plays_without_a_search() -> None:
    """A linked better copy jumps the pinned one, and nothing is searched for."""
    local = _provider(LOCAL, streaming=False)
    mass = _mass([local, _provider(TIDAL)])
    audio = StreamsAudio(mass)
    mp3 = _mapping(LOCAL, LOCAL_ID, MP3)

    streamdetails = await audio.get_stream_details(
        _queue_item(mp3, _mapping(TIDAL, TIDAL_ID, FLAC), pinned=mp3)
    )

    assert streamdetails.provider == TIDAL
    local.get_stream_details.assert_not_awaited()
    mass.music.tracks.match_provider.assert_not_awaited()


async def test_a_copy_reaching_the_upgrade_quality_plays_without_a_search() -> None:
    """A linked copy at the upgrade quality goes before an own poorer one, and no search runs."""
    local = _provider(LOCAL, streaming=False)
    spotify = _provider(SPOTIFY)
    tidal = _provider(TIDAL)
    mass = _mass([local, spotify, tidal], owned=(SPOTIFY,))
    audio = StreamsAudio(mass)
    mp3 = _mapping(LOCAL, LOCAL_ID, AudioFormat(content_type=ContentType.MP3, bit_rate=128))

    streamdetails = await audio.get_stream_details(
        _queue_item(
            mp3,
            _mapping(SPOTIFY, SPOTIFY_ID, OGG),
            _mapping(TIDAL, TIDAL_ID, FLAC),
            pinned=mp3,
        )
    )

    assert streamdetails.provider == TIDAL
    spotify.get_stream_details.assert_not_awaited()
    mass.music.tracks.match_provider.assert_not_awaited()


async def test_a_found_copy_of_unknown_quality_does_not_stop_the_upgrade() -> None:
    """An own account's match of unknown quality is kept for later, the lossless match plays."""
    yandex = _provider(YANDEX)
    tidal = _provider(TIDAL)
    mass = _mass([_provider(LOCAL, streaming=False), yandex, tidal], owned=(YANDEX,))
    yandex_match = _mapping(YANDEX, "yandex-1", AudioFormat())
    tidal_match = _mapping(TIDAL, TIDAL_ID, FLAC)
    found = {YANDEX: [yandex_match], TIDAL: [tidal_match]}
    mass.music.tracks.match_provider = AsyncMock(
        side_effect=lambda _track, provider, **_kwargs: found[provider.instance_id]
    )
    audio = StreamsAudio(mass)

    streamdetails = await audio.get_stream_details(_queue_item(_mapping(LOCAL, LOCAL_ID, MP3)))

    assert streamdetails.provider == TIDAL
    yandex.get_stream_details.assert_not_awaited()
    await asyncio.sleep(0)
    mass.music.tracks.add_unclaimed_provider_mappings.assert_awaited_once_with(
        LIBRARY_ID, [yandex_match, tidal_match]
    )


async def test_a_found_copy_of_unknown_quality_alone_leaves_the_chosen_copy() -> None:
    """A match of unknown quality is no known upgrade: it is stored, the chosen copy plays."""
    yandex = _provider(YANDEX)
    mass = _mass([_provider(LOCAL, streaming=False), yandex], owned=(YANDEX,))
    yandex_match = _mapping(YANDEX, "yandex-1", AudioFormat())
    mass.music.tracks.match_provider = AsyncMock(return_value=[yandex_match])
    audio = StreamsAudio(mass)

    streamdetails = await audio.get_stream_details(_queue_item(_mapping(LOCAL, LOCAL_ID, MP3)))

    assert streamdetails.provider == LOCAL
    yandex.get_stream_details.assert_not_awaited()
    await asyncio.sleep(0)
    mass.music.tracks.add_unclaimed_provider_mappings.assert_awaited_once_with(
        LIBRARY_ID, [yandex_match]
    )


async def test_the_search_goes_on_past_a_match_below_the_upgrade_quality() -> None:
    """A poorer match is kept, and the next service is searched for one that is good enough."""
    spotify = _provider(SPOTIFY)
    tidal = _provider(TIDAL)
    mass = _mass([_provider(LOCAL, streaming=False), spotify, tidal])
    spotify_match = _mapping(SPOTIFY, SPOTIFY_ID, OGG)
    tidal_match = _mapping(TIDAL, TIDAL_ID, FLAC)
    found = {SPOTIFY: [spotify_match], TIDAL: [tidal_match]}
    mass.music.tracks.match_provider = AsyncMock(
        side_effect=lambda _track, provider, **_kwargs: found[provider.instance_id]
    )
    audio = StreamsAudio(mass)

    streamdetails = await audio.get_stream_details(_queue_item(_mapping(LOCAL, LOCAL_ID, MP3)))

    assert streamdetails.provider == TIDAL
    assert mass.music.tracks.match_provider.await_count == 2
    await asyncio.sleep(0)
    mass.music.tracks.add_unclaimed_provider_mappings.assert_awaited_once_with(
        LIBRARY_ID, [spotify_match, tidal_match]
    )


async def test_a_service_without_a_free_stream_slot_is_searched_as_well() -> None:
    """A service busy streaming the track before this one still has its better copy found."""
    tidal = _provider(TIDAL)
    tidal.has_available_stream_slot = False
    mass = _mass([_provider(LOCAL, streaming=False), tidal])
    mass.music.tracks.match_provider = AsyncMock(return_value=[_mapping(TIDAL, TIDAL_ID, FLAC)])
    audio = StreamsAudio(mass)

    streamdetails = await audio.get_stream_details(_queue_item(_mapping(LOCAL, LOCAL_ID, MP3)))

    # the stream slot is only taken once the stream starts, after the current track is done
    assert streamdetails.provider == TIDAL
    mass.music.tracks.match_provider.assert_awaited_once()


@pytest.mark.parametrize("media_type", [MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE])
async def test_an_audiobook_or_episode_keeps_its_pin_below_the_upgrade_quality(
    media_type: MediaType,
) -> None:
    """The pin keeps the progress on the account it was started from, whatever its quality."""
    started = "audiobookshelf--home"
    started_copy = _mapping(started, "book-1", AudioFormat(content_type=ContentType.MP3))
    better_copy = _mapping(TIDAL, "book-1", FLAC)
    providers = [_provider(started, streaming=False), _provider(TIDAL)]
    mass = _mass(providers)
    audio = StreamsAudio(mass)
    media_item: Audiobook | PodcastEpisode = (
        Audiobook(
            item_id=LIBRARY_ID,
            provider="library",
            name="Book",
            provider_mappings={started_copy, better_copy},
        )
        if media_type == MediaType.AUDIOBOOK
        else PodcastEpisode(
            item_id="book-1",
            provider=started,
            name="Episode",
            provider_mappings={started_copy, better_copy},
            position=1,
            podcast=ItemMapping(
                media_type=MediaType.PODCAST, item_id="show", provider=started, name="Show"
            ),
        )
    )
    queue_item = QueueItem(
        queue_id="q1",
        queue_item_id="qi1",
        name="Book",
        duration=None,
        media_item=media_item,
        origin=QueueItemOrigin(provider_instance=started, item_id="book-1"),
    )

    streamdetails = await audio.get_stream_details(queue_item)

    assert streamdetails.provider == started
    providers[1].get_stream_details.assert_not_awaited()
    mass.music.tracks.match_provider.assert_not_awaited()
