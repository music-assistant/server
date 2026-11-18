"""Tests for the MusicProvider base model."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.media_items import BrowseFolder
from music_assistant_models.player_queue import PlayerQueue

import music_assistant.models.music_provider as music_provider_mod
from music_assistant.models.music_provider import (
    DEFAULT_MAX_CONCURRENT_STREAMS,
    MusicProvider,
    ProviderStreamHeldError,
    ProviderStreamLimitError,
)


class _StreamingProvider(MusicProvider):
    """Streaming provider using the default source limit."""


class _NonStreamingProvider(MusicProvider):
    """Non-streaming provider with no source limit."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return False for this local provider."""
        return False


class _SingleStreamProvider(MusicProvider):
    """Streaming provider with one source slot."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return one source slot."""
        return 1


class _TwoStreamProvider(MusicProvider):
    """Streaming provider with two source slots."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return two source slots."""
        return 2


def _make_provider[ProviderT: MusicProvider](
    provider_cls: type[ProviderT], supported_features: set[ProviderFeature] | None = None
) -> ProviderT:
    """Construct a minimal MusicProvider instance."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test_provider"
    manifest.name = "Test Provider"
    config = MagicMock()
    config.name = "Test Provider"
    config.instance_id = "test_provider--1"
    config.get_value.return_value = "GLOBAL"
    return provider_cls(mass, manifest, config, supported_features)


def test_supported_media_types_default_follows_library_features() -> None:
    """Without an override, media type support is derived from the library features."""
    provider = _make_provider(
        _StreamingProvider,
        supported_features={
            ProviderFeature.SEARCH,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
        },
    )

    assert provider.supported_media_types == {MediaType.TRACK, MediaType.PLAYLIST}


async def test_browse_root_library_folders_play_only_tracks() -> None:
    """Of the library folders at the browse root, only the tracks folder is playable."""
    provider = _make_provider(
        _StreamingProvider,
        supported_features={
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
        },
    )

    folders = await provider.browse("test_provider--1://")

    assert {f.item_id: f.is_playable for f in folders if isinstance(f, BrowseFolder)} == {
        "artists": False,
        "albums": False,
        "tracks": True,
        "playlists": False,
    }


def test_streaming_provider_has_conservative_default() -> None:
    """Unknown streaming providers default to five concurrent sources."""
    provider = _make_provider(_StreamingProvider)

    assert provider.max_concurrent_streams == DEFAULT_MAX_CONCURRENT_STREAMS == 5


def test_non_streaming_provider_is_unlimited() -> None:
    """Non-streaming providers do not allocate a source limiter."""
    provider = _make_provider(_NonStreamingProvider)

    assert provider.max_concurrent_streams is None
    assert provider.has_available_stream_slot


async def test_stream_slot_is_scoped_per_provider_instance() -> None:
    """One busy provider instance does not consume another instance's slot."""
    first = _make_provider(_SingleStreamProvider)
    second = _make_provider(_SingleStreamProvider)
    cast("MagicMock", second.config).instance_id = "test_provider--2"

    async with first.acquire_stream_slot(0.1):
        assert not first.has_available_stream_slot
        assert second.has_available_stream_slot
        with pytest.raises(ProviderStreamLimitError) as exc_info:
            async with first.acquire_stream_slot(0):
                pytest.fail("A second source slot was unexpectedly acquired")
        # the error must carry the localizable reason (provider name + limit) for API clients
        assert exc_info.value.translation_key == "provider_stream_limit"
        assert exc_info.value.translation_args == ["Test Provider", 1]

    assert first.has_available_stream_slot


def test_a_held_stream_slot_names_the_player_and_stays_a_capacity_error() -> None:
    """The refusal tells the user and the client which player holds the slot."""
    provider = _make_provider(_SingleStreamProvider)
    holder = PlayerQueue(
        queue_id="kitchen", active=True, display_name="Kitchen", available=True, items=1
    )

    err = ProviderStreamHeldError(provider, holder)

    assert isinstance(err, ProviderStreamLimitError)
    assert err.error_code == 29
    assert str(err) == "Test Provider is already playing on Kitchen"
    assert err.translation_key == "provider_stream_held"
    assert err.translation_args == ["Test Provider", "Kitchen"]
    assert err.data == {
        "queue_id": "kitchen",
        "player_name": "Kitchen",
        "provider_instance": "test_provider--1",
    }
    assert err.provider_instance == "test_provider--1"
    assert err.limit == 1


async def test_stream_slot_is_granted_once_released() -> None:
    """A waiter within its timeout is served as soon as the active source releases."""
    provider = _make_provider(_SingleStreamProvider)
    acquired = asyncio.Event()

    async def _wait_for_slot() -> None:
        async with provider.acquire_stream_slot(5):
            acquired.set()

    async with provider.acquire_stream_slot(5):
        waiter = asyncio.create_task(_wait_for_slot())
        await asyncio.sleep(0)
        assert not acquired.is_set()

    await waiter

    assert acquired.is_set()
    assert provider.has_available_stream_slot


async def test_cancelled_stream_slot_wait_does_not_consume_capacity() -> None:
    """Cancelling a waiter leaves the active source as the only leased slot."""
    provider = _make_provider(_SingleStreamProvider)

    async with provider.acquire_stream_slot(0.1):
        waiter = asyncio.create_task(provider.acquire_stream_slot(None).__aenter__())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not provider.has_available_stream_slot

    assert provider.has_available_stream_slot


async def _take(
    provider: MusicProvider,
    granted: list[str],
    name: str,
    queue_id: str | None = None,
    give_back: asyncio.Event | None = None,
    reserve: bool = False,
) -> None:
    """
    Take a slot, note that it was granted and hold it until told to give it back.

    :param provider: The provider whose slot to take.
    :param granted: Collects the names of the takers in the order they got their slot.
    :param name: The name of this taker.
    :param queue_id: The queue the slot is taken for, if any.
    :param give_back: Set by the test once the slot may go back; at once without one.
    :param reserve: Whether the slot stays reserved for the queue when it goes back.
    """
    async with provider.acquire_stream_slot(5, queue_id=queue_id, reserve=lambda: reserve):
        granted.append(name)
        if give_back is not None:
            await give_back.wait()


def _always() -> bool:
    """Answer yes."""
    return True


def _never() -> bool:
    """Answer no."""
    return False


async def _turns() -> None:
    """Let everything that can run without waiting for time run."""
    for _ in range(5):
        await asyncio.sleep(0)


def _in_line(provider: MusicProvider) -> int:
    """Return how many takers the provider still has waiting for a slot."""
    slots = provider._stream_slots
    assert slots is not None
    return len(slots._waiters)


async def test_takers_are_served_in_the_order_they_asked() -> None:
    """Whoever waits longest for a slot gets the next one."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []

    async with provider.acquire_stream_slot(0):
        waiters = []
        for name in ("first", "second", "third"):
            waiters.append(asyncio.create_task(_take(provider, granted, name)))
            await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(*waiters), 1)

    assert granted == ["first", "second", "third"]
    assert provider.has_available_stream_slot


async def test_a_slot_given_back_stays_reserved_for_its_queue() -> None:
    """A taker that already waits does not get a slot that is kept for the queue that had it."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    give_back = asyncio.Event()
    source = asyncio.create_task(
        _take(provider, granted, "source", "queue_a", give_back, reserve=True)
    )
    await asyncio.sleep(0)
    other = asyncio.create_task(_take(provider, granted, "other"))
    await asyncio.sleep(0)

    give_back.set()
    await source
    await _turns()

    assert granted == ["source"]
    # no source is open, though the slot is spoken for
    assert provider.has_available_stream_slot
    assert provider.stream_slot_reservations == ("queue_a",)
    # the queue it is kept for takes it without waiting, and with that the slot is an
    # ordinary one again
    async with provider.acquire_stream_slot(0, queue_id="queue_a"):
        granted.append("next source")
    await asyncio.wait_for(other, 1)
    assert granted == ["source", "next source", "other"]
    assert not provider.stream_slot_reservations


async def test_a_slot_is_only_kept_when_its_taker_says_so() -> None:
    """A slot that goes back without a reservation is handed to whoever waits."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    give_back = asyncio.Event()
    source = asyncio.create_task(_take(provider, granted, "source", "queue_a", give_back))
    await asyncio.sleep(0)
    other = asyncio.create_task(_take(provider, granted, "other"))
    await asyncio.sleep(0)

    give_back.set()
    await source
    await asyncio.wait_for(other, 1)

    assert granted == ["source", "other"]


async def test_the_queue_a_slot_is_kept_for_goes_ahead_of_takers_that_waited_longer() -> None:
    """A request of the queue that gave the slot back gets it before an earlier taker."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    give_back = asyncio.Event()
    source = asyncio.create_task(
        _take(provider, granted, "source", "queue_a", give_back, reserve=True)
    )
    await asyncio.sleep(0)
    other = asyncio.create_task(_take(provider, granted, "other"))
    await asyncio.sleep(0)
    next_source = asyncio.create_task(_take(provider, granted, "next source", "queue_a"))
    await asyncio.sleep(0)

    give_back.set()
    await asyncio.wait_for(asyncio.gather(source, next_source, other), 1)

    assert granted == ["source", "next source", "other"]


async def test_a_reservation_lapses_by_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slot its queue did not come back for goes to the taker that waits, within its wait."""
    monkeypatch.setattr(music_provider_mod, "STREAM_SLOT_RESERVATION", 0.05)
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    give_back = asyncio.Event()
    source = asyncio.create_task(
        _take(provider, granted, "source", "queue_a", give_back, reserve=True)
    )
    await asyncio.sleep(0)
    other = asyncio.create_task(_take(provider, granted, "other"))
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()

    given_back = loop.time()
    give_back.set()
    await source
    await asyncio.wait_for(other, 1)

    assert granted == ["source", "other"]
    assert loop.time() - given_back >= 0.04
    assert provider.has_available_stream_slot
    # nothing of the reservation is left: its queue has to ask like anyone else
    async with provider.acquire_stream_slot(0):
        with pytest.raises(ProviderStreamLimitError):
            async with provider.acquire_stream_slot(0, queue_id="queue_a"):
                pytest.fail("A slot was handed out beyond the limit")


async def test_a_reservation_that_is_ended_frees_its_slot_at_once() -> None:
    """Ending a queue's reservation hands the slot to the taker that waits."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    async with provider.acquire_stream_slot(0, queue_id="queue_a", reserve=lambda: True):
        other = asyncio.create_task(_take(provider, granted, "other"))
        await asyncio.sleep(0)
    await _turns()
    assert granted == []

    provider.end_stream_slot_reservation("queue_b")
    await _turns()
    assert granted == []
    provider.end_stream_slot_reservation("queue_a")
    # far inside the time a reservation lasts
    await asyncio.wait_for(other, 2)

    assert granted == ["other"]
    assert provider.has_available_stream_slot


async def test_a_queue_is_never_held_by_its_own_reservation() -> None:
    """A slot kept for a queue is free to that queue and taken to every other one."""
    provider = _make_provider(_SingleStreamProvider)

    async with provider.acquire_stream_slot(0, queue_id="queue_a", reserve=lambda: True):
        pass

    with pytest.raises(ProviderStreamLimitError):
        async with provider.acquire_stream_slot(0, queue_id="queue_b"):
            pytest.fail("Another queue got the slot that is kept for queue_a")
    with pytest.raises(ProviderStreamLimitError):
        async with provider.acquire_stream_slot(0):
            pytest.fail("A taker without a queue got the slot that is kept for queue_a")
    async with provider.acquire_stream_slot(0, queue_id="queue_a"):
        assert not provider.has_available_stream_slot
    assert provider.has_available_stream_slot


async def test_a_reservation_takes_one_slot_of_a_provider_with_more() -> None:
    """One slot is kept for a queue, however many it gave back; the rest is free to anyone."""
    provider = _make_provider(_TwoStreamProvider)

    async with (
        provider.acquire_stream_slot(0, queue_id="queue_a", reserve=lambda: True),
        provider.acquire_stream_slot(0, queue_id="queue_a", reserve=lambda: True),
    ):
        pass

    async with provider.acquire_stream_slot(0, queue_id="queue_b"):
        with pytest.raises(ProviderStreamLimitError):
            async with provider.acquire_stream_slot(0):
                pytest.fail("A slot was handed out next to an open source and a reservation")
        async with provider.acquire_stream_slot(0, queue_id="queue_a"):
            assert not provider.has_available_stream_slot


async def test_a_wait_that_times_out_leaves_nothing_behind() -> None:
    """A taker that gave up is out of the line, behind an open source and behind a reservation."""
    provider = _make_provider(_SingleStreamProvider)

    async with provider.acquire_stream_slot(0, queue_id="queue_a", reserve=lambda: True):
        with pytest.raises(ProviderStreamLimitError):
            async with provider.acquire_stream_slot(0.02):
                pytest.fail("A second source slot was unexpectedly acquired")
        assert _in_line(provider) == 0
    with pytest.raises(ProviderStreamLimitError):
        async with provider.acquire_stream_slot(0.02, queue_id="queue_b"):
            pytest.fail("Another queue got the slot that is kept for queue_a")

    assert _in_line(provider) == 0
    assert provider.has_available_stream_slot
    # the reservation it waited behind is untouched
    async with provider.acquire_stream_slot(0, queue_id="queue_a"):
        pass


async def test_a_cancelled_wait_leaves_nothing_behind() -> None:
    """A taker that is cancelled while it waits is out of the line and took no slot."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []

    async with provider.acquire_stream_slot(0):
        waiter = asyncio.create_task(_take(provider, granted, "cancelled"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert _in_line(provider) == 0

    assert granted == []
    async with provider.acquire_stream_slot(0):
        assert not provider.has_available_stream_slot


async def test_a_slot_given_back_passes_over_a_wait_that_was_just_given_up() -> None:
    """A slot that goes back before a cancelled taker left the line goes to the next one."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []

    async with provider.acquire_stream_slot(0):
        first = asyncio.create_task(_take(provider, granted, "first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(_take(provider, granted, "second"))
        await asyncio.sleep(0)
        # cancelled, and still in line when the slot goes back in the same turn
        first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, 1)

    assert granted == ["second"]
    assert _in_line(provider) == 0
    assert provider.has_available_stream_slot


async def test_a_wait_cancelled_after_it_was_woken_passes_its_slot_on() -> None:
    """A taker that is cancelled in the moment it gets the slot hands it to the next in line."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []

    async with provider.acquire_stream_slot(0):
        first = asyncio.create_task(_take(provider, granted, "first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(_take(provider, granted, "second"))
        await asyncio.sleep(0)
    # the slot went to the first in line, which is cancelled before it could run
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, 1)

    assert granted == ["second"]
    assert _in_line(provider) == 0
    assert provider.has_available_stream_slot


async def test_the_slots_never_pass_the_limit_and_every_taker_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Takers with and without a queue, reservations and timeouts never open a third source."""
    monkeypatch.setattr(music_provider_mod, "STREAM_SLOT_RESERVATION", 0.01)
    provider = _make_provider(_TwoStreamProvider)
    open_sources = 0
    most_open = 0
    served: list[int] = []

    async def _use(index: int) -> None:
        nonlocal open_sources, most_open
        queue_id = (None, "queue_a", "queue_b")[index % 3]
        async with provider.acquire_stream_slot(
            None, queue_id=queue_id, reserve=lambda: index % 2 == 0
        ):
            open_sources += 1
            most_open = max(most_open, open_sources)
            await asyncio.sleep(0.001 * (index % 4))
            open_sources -= 1
        served.append(index)

    async def _give_up(index: int) -> None:
        with pytest.raises(ProviderStreamLimitError):
            async with provider.acquire_stream_slot(0.001 * index):
                # one that does get a slot keeps it longer than any of them waits
                await asyncio.sleep(1)

    async with provider.acquire_stream_slot(0), provider.acquire_stream_slot(0):
        takers = [asyncio.create_task(_use(index)) for index in range(40)]
        quitters = [asyncio.create_task(_give_up(index)) for index in range(1, 6)]
        await asyncio.gather(*quitters)
    await asyncio.wait_for(asyncio.gather(*takers), 10)

    assert most_open == 2
    assert sorted(served) == list(range(40))
    assert _in_line(provider) == 0
    assert provider.has_available_stream_slot


async def test_a_replaced_provider_object_starts_with_slots_of_its_own() -> None:
    """A source that is still open on a reloaded provider's old object costs the new one nothing."""
    replaced = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    give_back = asyncio.Event()
    source = asyncio.create_task(
        _take(replaced, granted, "source", "queue_a", give_back, reserve=True)
    )
    await asyncio.sleep(0)
    # the provider is reloaded: same instance, another object
    provider = _make_provider(_SingleStreamProvider)

    async with provider.acquire_stream_slot(0, queue_id="queue_b"):
        # the old source ends while the new object's slot is taken
        give_back.set()
        await source
    # the old object keeps its slot for the queue of that source, the new one keeps nothing
    async with provider.acquire_stream_slot(0, queue_id="queue_b"):
        with pytest.raises(ProviderStreamLimitError):
            async with replaced.acquire_stream_slot(0, queue_id="queue_b"):
                pytest.fail("Another queue got the slot the old object keeps for queue_a")
    # and what the old object keeps ends there, without a trace on the new one
    replaced.end_stream_slot_reservation("queue_a")
    async with replaced.acquire_stream_slot(0), provider.acquire_stream_slot(0):
        pass


async def test_a_wait_in_line_keeps_the_slot_for_its_queue() -> None:
    """A queue that waited in line finds the slot kept for it, ahead of a later taker."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    give_back = asyncio.Event()
    source = asyncio.create_task(_take(provider, granted, "source", "queue_a", give_back))
    await asyncio.sleep(0)
    waiting = asyncio.create_task(provider.wait_for_stream_slot(5, "queue_b", _never, _always))
    await _turns()
    later = asyncio.create_task(_take(provider, granted, "later"))
    await _turns()

    give_back.set()
    await source
    await asyncio.wait_for(waiting, 1)
    await _turns()

    assert granted == ["source"]
    assert provider.stream_slot_reservations == ("queue_b",)
    async with provider.acquire_stream_slot(0, queue_id="queue_b"):
        granted.append("queue_b")
    await asyncio.wait_for(later, 1)
    assert granted == ["source", "queue_b", "later"]


async def test_a_wait_in_line_is_refused_once_a_holder_has_audio() -> None:
    """A wait is asked again when a source takes the slot and when it gets audio, not before."""
    provider = _make_provider(_SingleStreamProvider)
    async with provider.acquire_stream_slot(0, queue_id="queue_a", reserve=lambda: True):
        pass
    asked: list[bool] = []
    holder = MagicMock(ready=asyncio.Event())

    def _refuse() -> bool:
        asked.append(holder.ready.is_set())
        if holder.ready.is_set():
            raise ProviderStreamLimitError(provider, None)
        return False

    waiting = asyncio.create_task(provider.wait_for_stream_slot(5, "queue_b", _refuse, _always))
    await _turns()
    assert asked == []
    async with provider.acquire_stream_slot(0, holder, queue_id="queue_a"):
        await _turns()
        assert asked == [False]
        holder.ready.set()
        with pytest.raises(ProviderStreamLimitError):
            await asyncio.wait_for(waiting, 1)

    assert asked == [False, True]
    assert _in_line(provider) == 0
    assert provider.has_available_stream_slot
    assert provider.stream_slot_reservations == ()


async def test_a_wait_in_line_that_is_given_up_leaves_without_a_slot() -> None:
    """A wait that ends without a slot leaves the line, and the slot goes to who waits next."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    async with provider.acquire_stream_slot(0, queue_id="queue_a", reserve=lambda: True):
        pass
    waiting = asyncio.create_task(provider.wait_for_stream_slot(5, "queue_b", _always, _always))
    await _turns()
    later = asyncio.create_task(_take(provider, granted, "later"))
    await _turns()

    async with provider.acquire_stream_slot(0, MagicMock(ready=asyncio.Event()), "queue_a"):
        await asyncio.wait_for(waiting, 1)
        assert _in_line(provider) == 1
    await asyncio.wait_for(later, 1)

    assert granted == ["later"]
    assert provider.stream_slot_reservations == ()


async def test_a_wait_in_line_notices_a_source_that_takes_the_slot_in_the_same_turn() -> None:
    """A source that takes the kept slot in the loop turn the wait starts wakes the wait too."""
    provider = _make_provider(_SingleStreamProvider)
    async with provider.acquire_stream_slot(0, queue_id="queue_a", reserve=lambda: True):
        pass
    holder = MagicMock(ready=asyncio.Event())
    asked: list[bool] = []

    def _refuse() -> bool:
        asked.append(holder.ready.is_set())
        if holder.ready.is_set():
            raise ProviderStreamLimitError(provider, None)
        return False

    async def _source_takes_the_slot() -> None:
        async with provider.acquire_stream_slot(0, holder, queue_id="queue_a"):
            await asyncio.Event().wait()

    waiting = asyncio.create_task(provider.wait_for_stream_slot(5, "queue_b", _refuse, _always))
    # its first step runs in the same loop turn, right after the first step of the wait
    source = asyncio.create_task(_source_takes_the_slot())
    try:
        await _turns()
        holder.ready.set()
        with pytest.raises(ProviderStreamLimitError):
            await asyncio.wait_for(waiting, 1)
        assert asked == [False, True]
    finally:
        source.cancel()
        await asyncio.gather(waiting, source, return_exceptions=True)


async def test_a_wait_in_line_that_no_longer_wants_the_slot_passes_it_on() -> None:
    """A slot that comes free for a wait whose queue no longer wants it goes to who waits next."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    async with provider.acquire_stream_slot(0, queue_id="queue_a", reserve=lambda: True):
        pass
    waiting = asyncio.create_task(provider.wait_for_stream_slot(5, "queue_b", _never, _never))
    await _turns()
    later = asyncio.create_task(_take(provider, granted, "later"))
    await _turns()

    provider.end_stream_slot_reservation("queue_a")
    await asyncio.wait_for(waiting, 1)
    await asyncio.wait_for(later, 1)

    assert granted == ["later"]
    assert provider.stream_slot_reservations == ()


async def test_a_wait_in_line_that_times_out_leaves_nothing_behind() -> None:
    """A wait in line that runs out of time leaves the line and keeps nothing."""
    provider = _make_provider(_SingleStreamProvider)
    granted: list[str] = []
    give_back = asyncio.Event()
    source = asyncio.create_task(_take(provider, granted, "source", "queue_a", give_back))
    await asyncio.sleep(0)

    with pytest.raises(ProviderStreamLimitError):
        await provider.wait_for_stream_slot(0.05, "queue_b", _never, _always)
    give_back.set()
    await source

    assert _in_line(provider) == 0
    assert provider.has_available_stream_slot
    assert provider.stream_slot_reservations == ()


def _listing(*positions: int) -> list[MagicMock]:
    """Return a mocked listing page whose items carry the given playlist positions."""
    return [MagicMock(position=position) for position in positions]


async def test_move_playlist_tracks_replays_single_moves() -> None:
    """Without an override, a block move becomes single moves over the counted playlist."""
    provider = _make_provider(_StreamingProvider)
    pages = {0: _listing(1, 2, 3), 1: _listing(4, 5)}
    provider.get_playlist_tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _id, page=0: pages.get(page, [])
    )
    provider.move_playlist_track = AsyncMock()  # type: ignore[method-assign]

    await provider.move_playlist_tracks("playlist_1", [4, 5], 1)

    assert [call.args for call in provider.move_playlist_track.await_args_list] == [
        ("playlist_1", 4, 1),
        ("playlist_1", 5, 2),
    ]


@pytest.mark.parametrize(
    ("listed", "positions", "to_position", "expected"),
    [
        pytest.param((1, 3), [3], 1, [("playlist_1", 3, 1)], id="skipped-middle-entry"),
        pytest.param((1, 3), [1], None, [("playlist_1", 1, 3)], id="to-end-past-skipped"),
        pytest.param((1, 2), [1], None, [("playlist_1", 1, 2)], id="to-end-of-listing"),
    ],
)
async def test_move_playlist_tracks_counts_entries_the_listing_leaves_out(
    listed: tuple[int, ...],
    positions: list[int],
    to_position: int | None,
    expected: list[tuple[str, int, int]],
) -> None:
    """Positions past the listed count are valid, and None moves the tracks to the end."""
    provider = _make_provider(_StreamingProvider)
    provider.get_playlist_tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _id, page=0: _listing(*listed) if page == 0 else []
    )
    provider.move_playlist_track = AsyncMock()  # type: ignore[method-assign]

    await provider.move_playlist_tracks("playlist_1", positions, to_position)

    assert [call.args for call in provider.move_playlist_track.await_args_list] == expected
