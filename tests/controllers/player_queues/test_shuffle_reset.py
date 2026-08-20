"""
Tests for the shuffle state a newly started media item ends up with.

Shuffle is a queue setting the user owns: it survives everything they play, except media that
carries an order of its own. Starting an album, podcast, episode, audiobook or audio source plays
it in that order and switches shuffle off with it, while a playlist or artist keeps whatever the
queue is set to. An explicit ``shuffle`` argument always wins, and the options that only stage
items leave the state alone. These drive the real ``play_media`` path against a bare controller
instance, mirroring ``test_user_initiated_plays`` and ``test_enqueue_options``: resolution and
playback are stubbed, but the enqueue/load path runs for real so the resulting item order is
verified end-to-end.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Audiobook,
    AudioSource,
    ItemMapping,
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

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.constants import ORDERED_MEDIA_TYPES
from music_assistant.controllers.player_queues.state import PlayerQueueData

# the album the user starts, in its own track order
ALBUM_TRACKS = ["t1", "t2", "t3", "t4"]

# the options that start the media right away, i.e. begin a new listening session
START_OPTIONS = [QueueOption.PLAY, QueueOption.REPLACE]

# the options that stage media onto the queue instead of starting it
STAGE_OPTIONS = [QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT]

# a queue as an earlier shuffle left it: the list order deliberately disagrees with the sort order
SHUFFLED_QUEUE = [("e1", 0), ("e4", 3), ("e2", 1), ("e5", 4), ("e3", 2)]

# the managed pool a dynamic queue is playing when other media is staged over it
POOL_TRACKS = ["p1", "p2", "p3"]


def _track(item_id: str) -> Track:
    """Build a playable Track on the 'test' provider."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=60,
        artists=UniqueList(
            [ItemMapping(item_id="a", provider="test", name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _album() -> Album:
    """Build the Album the user presses play on (its configured enqueue default is 'replace')."""
    return Album(
        item_id="al1",
        provider="test",
        name="Album al1",
        provider_mappings={
            ProviderMapping(item_id="al1", provider_domain="test", provider_instance="test")
        },
    )


def _playlist() -> Playlist:
    """Build a plain playlist: a pool of tracks with no running order of its own to protect."""
    return Playlist(
        item_id="pl1",
        provider="test",
        name="Playlist pl1",
        provider_mappings={
            ProviderMapping(item_id="pl1", provider_domain="test", provider_instance="test")
        },
    )


def _ordered_item(media_type: MediaType) -> Any:
    """Build a media item of one of the types that carry an order of their own."""
    cls = {
        MediaType.ALBUM: Album,
        MediaType.AUDIOBOOK: Audiobook,
        MediaType.PODCAST: Podcast,
        MediaType.PODCAST_EPISODE: PodcastEpisode,
        MediaType.AUDIO_SOURCE: AudioSource,
        MediaType.RADIO: Radio,
    }[media_type]
    kwargs: dict[str, Any] = {
        "item_id": "o1",
        "provider": "test",
        "name": f"Ordered {media_type.value}",
        "provider_mappings": {
            ProviderMapping(item_id="o1", provider_domain="test", provider_instance="test")
        },
    }
    if media_type == MediaType.PODCAST_EPISODE:
        kwargs["position"] = 1
        kwargs["podcast"] = _ordered_item(MediaType.PODCAST)
    return cls(**kwargs)


def _dynamic_playlist() -> Playlist:
    """Build a dynamic playlist: a source that supplies its own tracks and is always a smart mix."""
    playlist = Playlist(
        item_id="dyn1",
        provider="test",
        name="Dynamic",
        provider_mappings={
            ProviderMapping(item_id="dyn1", provider_domain="test", provider_instance="test")
        },
    )
    playlist.is_dynamic = True
    return playlist


def _controller(**queue_kwargs: Any) -> Any:
    """
    Build a bare controller driving ``play_media`` on a single queue "q1".

    The album's tracks come from a stubbed media resolver and the shuffle is made deterministic
    (it reverses the batch), so the resulting queue order tells shuffle-on from shuffle-off.

    :param queue_kwargs: Overrides for the queue this controller is set up with.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(return_value=Mock(extra_data={}))
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_player_lock = Mock(return_value=lock_cm)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.get_config_value = Mock(return_value=QueueOption.REPLACE.value)  # type: ignore[method-assign]
    ctrl._managed_pool = Mock()
    ctrl._managed_pool.fill = AsyncMock(
        side_effect=lambda *_args, **_kwargs: [_track(item_id) for item_id in ALBUM_TRACKS]
    )
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=True)
    ctrl._smart_shuffle.arrange = AsyncMock(side_effect=lambda _queue, items: list(items)[::-1])
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        side_effect=lambda *_args, **_kwargs: [_track(item_id) for item_id in ALBUM_TRACKS]
    )
    queue = PlayerQueue(
        queue_id="q1", active=True, display_name="Q1", available=True, items=0, **queue_kwargs
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return ctrl


def _queue(ctrl: Any) -> PlayerQueue:
    """Return the controller's queue."""
    return cast("PlayerQueue", ctrl._queue_data["q1"].queue)


def _played_order(ctrl: Any) -> list[str]:
    """Return the item ids of the tracks currently loaded in the queue, in play order."""
    return [
        item.media_item.item_id
        for item in ctrl._queue_data["q1"].items
        if item.media_item is not None
    ]


def _load_shuffled_queue(ctrl: Any) -> None:
    """Fill the queue with items whose list order disagrees with their original sort order."""
    items = []
    for item_id, sort_index in SHUFFLED_QUEUE:
        item = QueueItem.from_media_item("q1", _track(item_id))
        item.sort_index = sort_index
        items.append(item)
    ctrl._queue_data["q1"].items = items
    _queue(ctrl).items = len(items)


def _load_dynamic_pool(ctrl: Any) -> None:
    """Fill the queue with a dynamic queue's managed pool, its first item playing."""
    ctrl._queue_data["q1"].items = [
        QueueItem.from_media_item("q1", _track(item_id)) for item_id in POOL_TRACKS
    ]
    queue = _queue(ctrl)
    queue.items = len(POOL_TRACKS)
    queue.current_index = 0


@pytest.mark.parametrize("option", START_OPTIONS)
async def test_playing_a_playlist_keeps_the_queue_shuffle(option: QueueOption) -> None:
    """A playlist is a pool of tracks, so it plays the way the queue's shuffle is set."""
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True)

    await ctrl.play_media("q1", _playlist(), option)

    assert _queue(ctrl).shuffle_enabled is True
    assert _queue(ctrl).smart_shuffle_active is True
    # the (reversed) shuffle order rather than the order the resolver handed them over in
    assert _played_order(ctrl) == ALBUM_TRACKS[::-1]
    # shuffle was settled before the items were resolved: a shuffled queue asks the resolver to
    # keep the items preceding a chosen track, an in-order one does not
    resolve_call = ctrl._media_resolver._resolve_media_items.call_args
    assert resolve_call.kwargs["keep_preceding_items"] is True


@pytest.mark.parametrize("option", START_OPTIONS)
async def test_the_queue_shuffle_survives_repeated_plays(option: QueueOption) -> None:
    """
    Shuffle belongs to the queue, so every playlist the user starts after it is shuffled too.

    The shuffle toggle is a setting the user owns, not a one-shot gesture attached to a single
    play: switching it on once has to keep shuffling whatever they pick next, however many things
    they play, or the toggle would silently switch itself off behind their back.
    """
    ctrl = _controller()
    await ctrl.set_shuffle("q1", True)
    await ctrl.play_media("q1", _playlist(), option)

    await ctrl.play_media("q1", _playlist(), option)

    assert _queue(ctrl).shuffle_enabled is True
    # still in effect when the second batch is resolved, not just left set on the queue
    resolve_call = ctrl._media_resolver._resolve_media_items.call_args
    assert resolve_call.kwargs["keep_preceding_items"] is True
    assert _played_order(ctrl) != ALBUM_TRACKS


@pytest.mark.parametrize("media_type", ORDERED_MEDIA_TYPES)
@pytest.mark.parametrize("option", START_OPTIONS)
async def test_starting_ordered_media_switches_shuffle_off(
    option: QueueOption, media_type: MediaType
) -> None:
    """Media sequenced by its author plays in that order, and takes the queue's shuffle with it."""
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True)

    await ctrl.play_media("q1", _ordered_item(media_type), option)

    assert _queue(ctrl).shuffle_enabled is False
    assert _queue(ctrl).smart_shuffle_active is False
    assert _played_order(ctrl) == ALBUM_TRACKS
    resolve_call = ctrl._media_resolver._resolve_media_items.call_args
    assert resolve_call.kwargs["keep_preceding_items"] is False


async def test_an_album_switches_shuffle_off_for_a_derived_enqueue_option() -> None:
    """The album rule also applies when the enqueue option comes from its config default."""
    ctrl = _controller(shuffle_enabled=True)

    await ctrl.play_media("q1", _album())

    # the option was derived from the album's configured default (which is 'replace')
    assert ctrl.get_config_value.call_args.args[0] == "default_enqueue_option_album"
    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


async def test_an_album_restores_the_order_of_the_items_that_stay_in_the_queue() -> None:
    """
    An album played onto a shuffled queue puts the items it keeps back in their original order.

    Unlike a replace, a play keeps the tail behind the current item, so switching shuffle off has
    to un-shuffle that tail as well: items left in shuffled order behind a queue that now reads
    "shuffle off" would contradict its own flag.
    """
    ctrl = _controller(shuffle_enabled=True, current_index=0)
    _load_shuffled_queue(ctrl)

    await ctrl.play_media("q1", _album(), QueueOption.PLAY)

    assert _queue(ctrl).shuffle_enabled is False
    # the item being played, then the album, then the kept tail back in its original order
    assert _played_order(ctrl) == ["e1", *ALBUM_TRACKS, "e2", "e3", "e4", "e5"]


async def test_an_unshuffled_queue_stays_unshuffled_for_a_playlist() -> None:
    """A playlist follows the queue's shuffle in both directions, so an off queue stays off."""
    ctrl = _controller(shuffle_enabled=False)

    await ctrl.play_media("q1", _playlist(), QueueOption.REPLACE)

    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


@pytest.mark.parametrize(
    ("batch", "expected"),
    [([_album(), _playlist()], False), ([_playlist(), _album()], True)],
    ids=["album-first", "playlist-first"],
)
async def test_the_first_item_of_a_batch_decides_for_the_whole_batch(
    batch: list[Any], expected: bool
) -> None:
    """
    A batch is judged by its first item: it is the only media type known this early.

    The shuffle state has to be settled before any of the items are resolved, because a shuffled
    queue resolves a chosen start item differently. Both orderings are checked: an album behind a
    playlist must not reach back and switch the queue's shuffle off.
    """
    ctrl = _controller(shuffle_enabled=True)

    await ctrl.play_media("q1", batch, QueueOption.REPLACE)

    assert _queue(ctrl).shuffle_enabled is expected


async def test_an_unresolvable_first_item_leaves_the_decision_to_the_next_one() -> None:
    """A batch is judged by the first item that resolves, not by one that could not be fetched."""
    ctrl = _controller(shuffle_enabled=True)
    ctrl.mass.music.get_item_by_uri = AsyncMock(side_effect=[MediaNotFoundError("gone"), _album()])

    await ctrl.play_media("q1", ["test://track/gone", "test://album/al1"], QueueOption.REPLACE)

    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


@pytest.mark.parametrize("media_type", ORDERED_MEDIA_TYPES)
async def test_explicit_shuffle_wins_over_the_medias_own_order(media_type: MediaType) -> None:
    """
    A caller asking for a shuffled play gets one, even for media with an order of its own.

    This is the "play shuffled" action every client offers: it has to be honoured whatever the
    queue's toggle says and whatever is being started, or the action would silently do nothing.
    """
    ctrl = _controller()

    await ctrl.play_media("q1", _ordered_item(media_type), QueueOption.REPLACE, shuffle=True)

    assert _queue(ctrl).shuffle_enabled is True
    assert _played_order(ctrl) == ALBUM_TRACKS[::-1]


@pytest.mark.parametrize("option", START_OPTIONS)
async def test_explicit_no_shuffle_wins_over_the_queue_shuffle(option: QueueOption) -> None:
    """An explicit "play in order" beats the shuffle the queue is set to."""
    ctrl = _controller(shuffle_enabled=True)

    await ctrl.play_media("q1", _playlist(), option, shuffle=False)

    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


async def test_dynamic_source_overrides_an_explicit_play_in_order() -> None:
    """A dynamic source is an always-on smart mix, so it outranks an explicit "play in order"."""
    ctrl = _controller()

    await ctrl.play_media("q1", _dynamic_playlist(), QueueOption.REPLACE, shuffle=False)

    assert _queue(ctrl).is_dynamic is True
    assert _queue(ctrl).shuffle_enabled is True
    assert _queue(ctrl).smart_shuffle_active is True


async def test_replacing_a_dynamic_queue_drops_the_smart_mix_indicator() -> None:
    """
    An album started over a dynamic queue is a plain queue, so it must not report a smart mix.

    The dynamic source is what made smart shuffle active here (the per-queue setting is off), so
    dropping it has to take the indicator with it.
    """
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True, is_dynamic=True)
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE, shuffle=True)

    assert _queue(ctrl).is_dynamic is False
    # the caller asked for a shuffled play, but a plain random shuffle is not a smart mix
    assert _queue(ctrl).shuffle_enabled is True
    assert _queue(ctrl).smart_shuffle_active is False


async def test_playing_over_a_dynamic_queue_honours_an_explicit_play_in_order() -> None:
    """
    An album played over a dynamic queue takes over from it, so it plays in the order asked for.

    Play does not clear the queue up front, so the queue is still dynamic when the shuffle state is
    settled - and a dynamic queue's toggle is locked, so the request cannot be routed through
    set_shuffle. The requested state still has to reach the items, which are resolved against it.
    """
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True, is_dynamic=True)

    await ctrl.play_media("q1", _album(), QueueOption.PLAY, shuffle=False)

    # the album took over as the queue's only source, so the queue is a plain one again
    assert _queue(ctrl).is_dynamic is False
    assert _queue(ctrl).shuffle_enabled is False
    assert _queue(ctrl).smart_shuffle_active is False
    # the flag alone proves little here: the album itself has to come out in its own order
    assert _played_order(ctrl) == ALBUM_TRACKS


@pytest.mark.parametrize("option", START_OPTIONS)
async def test_taking_over_a_dynamic_queue_drops_the_imposed_shuffle(option: QueueOption) -> None:
    """
    Media started over a dynamic queue does not inherit the shuffle that queue was forced into.

    A dynamic queue is an always-on smart mix, so its shuffle is imposed by the source rather than
    chosen by the user - its toggle is locked. Now that shuffle survives from one play to the next,
    a shuffle left latched on here would keep reordering everything the user plays afterwards.
    """
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True, is_dynamic=True)
    _load_dynamic_pool(ctrl)

    await ctrl.play_media("q1", _playlist(), option)

    assert _queue(ctrl).is_dynamic is False
    assert _queue(ctrl).shuffle_enabled is False
    assert _queue(ctrl).smart_shuffle_active is False


async def test_a_dynamic_queues_shuffle_is_dropped_even_if_nothing_resolves() -> None:
    """
    The source is replaced whether or not the media resolves, so its shuffle goes either way.

    The media type is only known once an item resolves, so the shuffle state is settled inside the
    resolve loop - which a batch that yields nothing never reaches.
    """
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True, is_dynamic=True)
    _load_dynamic_pool(ctrl)
    ctrl.mass.music.get_item_by_uri = AsyncMock(side_effect=MediaNotFoundError("gone"))

    with pytest.raises(MediaNotFoundError):
        await ctrl.play_media("q1", "test://track/gone", QueueOption.REPLACE_NEXT)

    assert _queue(ctrl).is_dynamic is False
    assert _queue(ctrl).shuffle_enabled is False
    assert _queue(ctrl).smart_shuffle_active is False


async def test_replace_next_over_a_dynamic_queue_drops_the_imposed_shuffle() -> None:
    """
    An album staged over a dynamic queue takes its source away, so the smart mix's shuffle goes too.

    Staging normally leaves the shuffle state alone, but the shuffle a dynamic queue runs on is not
    the user's own choice - its toggle is locked - so it must not silently reorder the album that
    replaces it. Replace next is the only staging option that replaces the queue's sources.
    """
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True, is_dynamic=True)
    _load_dynamic_pool(ctrl)

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE_NEXT)

    assert _queue(ctrl).is_dynamic is False
    assert _queue(ctrl).shuffle_enabled is False
    assert _queue(ctrl).smart_shuffle_active is False
    # the item playing, then the album in its own track order rather than the (reversed) shuffle
    assert _played_order(ctrl) == ["p1", *ALBUM_TRACKS]
    # settled before the items were resolved: a shuffled queue asks the resolver to keep the items
    # preceding a chosen track, an in-order one does not
    resolve_call = ctrl._media_resolver._resolve_media_items.call_args
    assert resolve_call.kwargs["keep_preceding_items"] is False


async def test_replace_next_that_leaves_the_queue_dynamic_keeps_the_smart_mix() -> None:
    """Staging another dynamic source keeps the queue an always-on smart mix, shuffle included."""
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True, is_dynamic=True)
    _load_dynamic_pool(ctrl)

    await ctrl.play_media("q1", _dynamic_playlist(), QueueOption.REPLACE_NEXT)

    assert _queue(ctrl).is_dynamic is True
    assert _queue(ctrl).shuffle_enabled is True
    assert _queue(ctrl).smart_shuffle_active is True


async def test_playing_an_album_on_an_ended_queue_switches_shuffle_off() -> None:
    """A finished queue is started over by a play, and an album starts it over in its own order."""
    ctrl = _controller(shuffle_enabled=True, ended=True)

    await ctrl.play_media("q1", _album(), QueueOption.PLAY)

    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


async def test_playing_a_playlist_on_an_ended_queue_keeps_shuffle() -> None:
    """Restarting a finished queue is still not a reason to change a setting the user owns."""
    ctrl = _controller(shuffle_enabled=True, ended=True)

    await ctrl.play_media("q1", _playlist(), QueueOption.PLAY)

    assert _queue(ctrl).shuffle_enabled is True
    assert _played_order(ctrl) == ALBUM_TRACKS[::-1]


@pytest.mark.parametrize("option", STAGE_OPTIONS)
async def test_staging_media_onto_an_ended_queue_keeps_shuffle(option: QueueOption) -> None:
    """
    Staging media onto a finished queue keeps its shuffle, even though the queue restarts.

    This is deliberate, not an oversight: the shuffle is only reset for the options the user reaches
    for to start something now. Whether the queue happens to have played to its end does not change
    what "queue this up" means, so it must not change the shuffle state either.
    """
    ctrl = _controller(shuffle_enabled=True, ended=True)

    await ctrl.play_media("q1", _album(), option)

    assert _queue(ctrl).shuffle_enabled is True
    # the batch landed in shuffle order instead of the album's own track order
    assert sorted(_played_order(ctrl)) == sorted(ALBUM_TRACKS)
    assert _played_order(ctrl) != ALBUM_TRACKS


@pytest.mark.parametrize("shuffle_enabled", [True, False])
@pytest.mark.parametrize("option", STAGE_OPTIONS)
async def test_enqueueing_leaves_shuffle_untouched(
    option: QueueOption, shuffle_enabled: bool
) -> None:
    """
    Staging media onto the queue is not a new listening session, so it keeps its shuffle.

    These all keep (part of) the existing queue, whose items are already in shuffled order, so
    switching shuffle off here would leave those items contradicting the queue's own flag.
    """
    # a running queue, as opposed to the finished one the staging options start over from
    ctrl = _controller(shuffle_enabled=shuffle_enabled, ended=False)

    await ctrl.play_media("q1", _album(), option)

    assert _queue(ctrl).shuffle_enabled is shuffle_enabled


@pytest.mark.parametrize("option", STAGE_OPTIONS)
async def test_an_explicit_shuffle_is_ignored_by_the_staging_options(option: QueueOption) -> None:
    """
    Only the options that start the media right away act on an explicit shuffle request.

    Staging keeps (part of) the existing queue, whose items are already in the order its current
    shuffle put them, so honouring a request here would leave them contradicting the toggle.
    """
    ctrl = _controller(shuffle_enabled=False)

    await ctrl.play_media("q1", _playlist(), option, shuffle=True)

    assert _queue(ctrl).shuffle_enabled is False


def test_clear_command_resets_shuffle() -> None:
    """Clearing the queue is an explicit "start over", so the shuffle goes with the content."""
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True)
    ctrl._queue_data["q1"].items = [
        QueueItem.from_media_item("q1", _track(item_id)) for item_id in ALBUM_TRACKS
    ]

    ctrl.clear("q1")

    assert _queue(ctrl).shuffle_enabled is False
    assert _queue(ctrl).smart_shuffle_active is False


def test_empty_queue_reaching_its_end_keeps_shuffle() -> None:
    """An end reached with nothing to replay is still not the user clearing the queue."""
    ctrl = _controller(shuffle_enabled=True)

    ctrl.mark_ended("q1")

    assert ctrl._queue_data["q1"].items == []
    assert _queue(ctrl).shuffle_enabled is True
