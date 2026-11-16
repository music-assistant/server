"""
Tests that playback keeps a restricted member from explicit content.

Playing an explicit item is refused when the member adds it. Playing an album, playlist or
artist enqueues the tracks the member may play, and the refills (radio mode and the managed
pool) leave explicit tracks out for the queue owner.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import MediaType, ProviderSharing, QueueOption
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    MediaItemMetadata,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.helpers import external_track_placeholder
from music_assistant.controllers.player_queues.managed_pool import ManagedPool
from music_assistant.controllers.player_queues.media_resolver import MediaResolver, ResolvedItem
from music_assistant.controllers.player_queues.state import PlayerQueueData
from tests.common import set_music_source_access

LOADER = "music_assistant.controllers.player_queues.queue_loader"
RESOLVER = "music_assistant.controllers.player_queues.media_resolver"
POOL = "music_assistant.controllers.player_queues.managed_pool"
INSTANCE = "deezer--member"
KID = User(user_id="kid", username="kid", role=UserRole.USER, allow_explicit=False)
PARENT = User(user_id="parent", username="parent", role=UserRole.USER)


def _track(item_id: str, explicit: bool | None) -> Track:
    """Build a playable track on the member's own source, with the given explicit flag."""
    return Track(
        item_id=item_id,
        provider=INSTANCE,
        name=f"Track {item_id}",
        duration=180,
        metadata=MediaItemMetadata(explicit=explicit),
        artists=UniqueList(
            [ItemMapping(item_id="a", provider=INSTANCE, name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="deezer", provider_instance=INSTANCE)
        },
    )


def _album() -> Album:
    """Build an album without an explicit flag, which may still hold explicit tracks."""
    return Album(
        item_id="album",
        provider=INSTANCE,
        name="Album",
        provider_mappings={
            ProviderMapping(item_id="album", provider_domain="deezer", provider_instance=INSTANCE)
        },
    )


def _clean() -> Track:
    """Build a fresh clean track; enqueueing slims a track in place, so none is shared."""
    return _track("clean", False)


def _explicit() -> Track:
    """Build a fresh explicit track; enqueueing slims a track in place, so none is shared."""
    return _track("explicit", True)


def _stored(uri: str) -> Track | Album:
    """Return the item the music source holds for a uri, with its own explicit flag."""
    item_id = uri.rsplit("/", 1)[-1]
    return _album() if item_id == "album" else _track(item_id, item_id == "explicit")


def _controller(user: User) -> Any:
    """
    Build a bare controller driving ``play_media`` on a single queue "q1" for the given user.

    The media resolver is a real one; what an item expands to is stubbed below it, so the
    resolver's own explicit filter runs.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(
        return_value=Mock(extra_data={}, offline_by_power_control=False)
    )
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_group_and_player_lock = Mock(return_value=lock_cm)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.get_config_value = Mock(return_value=QueueOption.REPLACE.value)  # type: ignore[method-assign]
    ctrl._managed_pool = Mock()
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    ctrl._media_resolver = MediaResolver(ctrl)
    ctrl._media_resolver._resolve_unfiltered_media_items = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda media_item, *_args, **_kwargs: (
            [ResolvedItem(_clean()), ResolvedItem(_explicit())]
            if isinstance(media_item, Album)
            else [ResolvedItem(media_item)]
        )
    )
    # the real source access check, over the member's own source
    music = MusicController.__new__(MusicController)
    music.mass = ctrl.mass
    ctrl.mass.music.check_item_playable_for_user = music.check_item_playable_for_user
    ctrl.mass.music.get_item_by_uri = AsyncMock(side_effect=_stored)
    ctrl.mass.providers = []
    set_music_source_access(
        ctrl.mass, {INSTANCE: ProviderAccess(owner=user.user_id, sharing=ProviderSharing.PRIVATE)}
    )
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return ctrl


def _queued_item_ids(ctrl: Any) -> list[str]:
    """Return the item ids of the tracks currently loaded in the queue."""
    return [item.media_item.item_id for item in ctrl._queue_data["q1"].items if item.media_item]


async def _play(ctrl: Any, user: User, item: Track | Album) -> None:
    """Play the item on the queue as the given user."""
    with (
        patch(f"{LOADER}.get_current_user", return_value=user),
        patch(f"{RESOLVER}.get_current_user", return_value=user),
    ):
        await ctrl.play_media("q1", item, QueueOption.REPLACE)


async def test_playing_an_explicit_track_is_refused() -> None:
    """A restricted member asking for an explicit track gets a clear error, nothing is queued."""
    ctrl = _controller(KID)

    with pytest.raises(MediaNotFoundError) as err:
        await _play(ctrl, KID, _explicit())

    assert err.value.translation_key == "media_explicit_not_allowed"
    assert _queued_item_ids(ctrl) == []


async def test_an_explicit_track_sent_without_its_flag_is_refused() -> None:
    """A track object the caller sent without its explicit flag is judged as it is stored."""
    ctrl = _controller(KID)

    with pytest.raises(MediaNotFoundError):
        await _play(ctrl, KID, _track("explicit", None))

    assert _queued_item_ids(ctrl) == []


async def test_a_track_found_by_its_external_id_is_checked_once_found() -> None:
    """A track known only by an external id is refused when the track found for it is explicit."""
    ctrl = _controller(KID)
    placeholder = external_track_placeholder("isrc:USRC17607839")
    queue_item = QueueItem(
        queue_id="q1", queue_item_id="1", name="x", duration=180, media_item=placeholder
    )
    ctrl._external_track_on_a_provider = AsyncMock(return_value=_explicit())

    with (
        patch(f"{LOADER}.resolve_playback_user_or_stand_in", AsyncMock(return_value=KID)),
        pytest.raises(MediaNotFoundError),
    ):
        await ctrl.look_up_external_track(queue_item)

    assert queue_item.media_item is placeholder


async def test_an_allowed_member_plays_an_explicit_track() -> None:
    """A member who may get explicit content plays it as before."""
    ctrl = _controller(PARENT)

    await _play(ctrl, PARENT, _explicit())

    assert _queued_item_ids(ctrl) == ["explicit"]


@pytest.mark.parametrize(("user", "expected"), [(KID, ["clean"]), (PARENT, ["clean", "explicit"])])
async def test_playing_a_container_enqueues_the_allowed_tracks(
    user: User, expected: list[str]
) -> None:
    """An album holding explicit tracks plays the tracks the member may play."""
    ctrl = _controller(user)

    await _play(ctrl, user, _album())

    assert _queued_item_ids(ctrl) == expected


async def _iterate(tracks: list[Track]) -> AsyncGenerator[Track]:
    """Yield the given tracks as the playlists controller does."""
    for track in tracks:
        yield track


@pytest.mark.parametrize("keep_preceding_items", [False, True], ids=["fast", "rotate"])
async def test_playlist_tracks_leave_the_explicit_ones_out(keep_preceding_items: bool) -> None:
    """The tracks to play for a playlist, also as an autoplay source, have no explicit one."""
    queues = MagicMock()
    resolver = MediaResolver(queues)
    queues.mass.music.playlists.iter_tracks = lambda *_args, **_kwargs: _iterate(
        [_clean(), _explicit()]
    )
    playlist = Playlist(item_id="list", provider=INSTANCE, name="List", provider_mappings=set())

    with patch(f"{RESOLVER}.get_current_user", return_value=KID):
        tracks = await resolver.get_playlist_tracks(
            playlist, start_item=None, keep_preceding_items=keep_preceding_items
        )

    assert [track.item_id for track in tracks] == ["clean"]


async def test_radio_mode_refill_leaves_explicit_tracks_out() -> None:
    """The similar tracks radio mode adds for the queue owner have no explicit one."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=1)
    queue_data = PlayerQueueData(queue=queue)
    seed = _track("seed", False)
    queue_data.items = [
        QueueItem(queue_id="q1", queue_item_id="s", name="s", duration=180, media_item=seed)
    ]
    queue_data.source_items = [seed]
    ctrl._queue_data = {"q1": queue_data}
    ctrl.logger = MagicMock()
    radio = MagicMock()
    radio.get_dynamic_tracks = AsyncMock(return_value=[_clean(), _explicit()])
    ctrl.mass = MagicMock()
    ctrl.mass.get_provider.return_value = radio

    with (
        patch(f"{LOADER}.playback_sources", AsyncMock(return_value=(None, None))),
        patch(f"{LOADER}.get_current_user", return_value=KID),
    ):
        tracks = await ctrl._get_similar_tracks("q1", is_initial=True)

    assert [track.item_id for track in tracks] == ["clean"]


def _pool() -> tuple[ManagedPool, MagicMock]:
    """Build a managed pool whose sources hand it a clean and an explicit track."""
    queues = MagicMock()
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[_clean(), _explicit()])
    queues.get_tracks_for_playback = AsyncMock(return_value=[_clean(), _explicit()])
    return ManagedPool(queues), queues


@pytest.mark.parametrize(("user", "expected"), [(KID, ["clean"]), (PARENT, ["clean", "explicit"])])
async def test_managed_pool_refills_leave_explicit_tracks_out(
    user: User, expected: list[str]
) -> None:
    """A finite source the pool draws on leaves explicit tracks out for the queue owner."""
    pool, _ = _pool()

    with patch(f"{POOL}.get_current_user", return_value=user):
        finite = await pool._fetch_tracks(MagicMock())

    assert [track.item_id for track in finite] == expected


async def test_a_finite_source_is_narrowed_again_when_the_queue_changes_hands() -> None:
    """The tracks a source saved for an earlier owner are narrowed for a restricted new one."""
    pool, queues = _pool()
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    queue_data = PlayerQueueData(queue=queue)
    queue_data.source_items = [_album()]
    queues.queue_data_or_none = Mock(return_value=queue_data)

    with patch(f"{POOL}.get_current_user", return_value=PARENT):
        before = await pool._collect_sources("q1", include_dynamic=False)
    with patch(f"{POOL}.get_current_user", return_value=KID):
        after = await pool._collect_sources("q1", include_dynamic=False)

    assert [track.item_id for track in before[0].candidates] == ["clean", "explicit"]
    assert [track.item_id for track in after[0].candidates] == ["clean"]


@pytest.mark.parametrize(("user", "expected"), [(KID, ["clean"]), (PARENT, ["clean", "explicit"])])
async def test_a_dynamic_station_batch_leaves_explicit_tracks_out(
    user: User, expected: list[str]
) -> None:
    """The next batch of a dynamic station leaves explicit tracks out for the queue owner."""
    queues = MagicMock()
    queues.mass.music.radio.dynamic_tracks = AsyncMock(return_value=[_clean(), _explicit()])
    station = Radio(item_id="station", provider=INSTANCE, name="Station", provider_mappings=set())

    with patch(f"{RESOLVER}.get_current_user", return_value=user):
        tracks = await MediaResolver(queues).get_dynamic_source_tracks(station)

    assert [track.item_id for track in tracks] == expected


def _episode(item_id: str, explicit: bool | None) -> PodcastEpisode:
    """Build an episode of a clean podcast on the member's own source, with its own flag."""
    podcast = Podcast(
        item_id="show",
        provider=INSTANCE,
        name="Show",
        metadata=MediaItemMetadata(explicit=False),
        provider_mappings={
            ProviderMapping(item_id="show", provider_domain="deezer", provider_instance=INSTANCE)
        },
    )
    return PodcastEpisode(
        item_id=item_id,
        provider=INSTANCE,
        name=item_id,
        podcast=podcast,
        position=1,
        duration=600,
        metadata=MediaItemMetadata(explicit=explicit),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="deezer", provider_instance=INSTANCE)
        },
    )


@pytest.mark.parametrize(("user", "queued"), [(KID, 0), (PARENT, 1)])
async def test_autoplay_skips_a_next_episode_the_owner_may_not_play(
    user: User, queued: int
) -> None:
    """Autoplay doesn't follow a clean episode with an explicit one for a restricted owner."""
    ctrl = _controller(user)
    ctrl._queue_data["q1"].queue.autoplay_enabled = True
    ctrl._media_resolver.get_next_podcast_episode = AsyncMock(
        return_value=_episode("explicit", True)
    )
    ctrl.load = AsyncMock()
    last_item = QueueItem(
        queue_id="q1",
        queue_item_id="1",
        name="clean",
        duration=600,
        media_item=_episode("clean", False),
    )

    with patch(f"{LOADER}.get_current_user", return_value=user):
        await ctrl._fill_autoplay_next_in_series("q1", last_item)

    assert ctrl.load.await_count == queued
