"""
Tests for the enqueue-time playback delegation in the queue loader.

A music provider may redirect playback of its items into a live external session
(``get_playback_delegate``, e.g. a Spotify Connect bridge). When every resolved item
redirects into the same session, the play request is handed to the provider
(``play_on_delegate``) and the MA queue plays the session's AudioSource instead (or,
for the ADD family, nothing at all — the items live in the session's own queue). A
mixed batch keeps the normally-streamable items and drops only those whose provider
cannot play without a delegate.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    AudioSource,
    MediaItemType,
    ProviderMapping,
    SourceQueueCapabilities,
    Track,
)

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.models.music_provider import MusicProvider

QUEUE_ID = "q1"


def _delegate_source(instance_id: str = "spotify_connect--a") -> AudioSource:
    """Build a queue-capable session AudioSource (redirect target)."""
    return AudioSource(
        item_id="main",
        provider=instance_id,
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id="main",
                provider_domain="spotify_connect",
                provider_instance=instance_id,
            )
        },
        queue_capabilities=SourceQueueCapabilities(
            provider_domain="spotify",
            playable_media_types=[
                MediaType.TRACK,
                MediaType.ALBUM,
                MediaType.PLAYLIST,
                MediaType.ARTIST,
            ],
            enqueueable_media_types=[MediaType.TRACK],
            can_shuffle=True,
            can_repeat=True,
        ),
    )


def _track(item_id: str, instance_id: str, domain: str) -> Track:
    """Build a resolved track carrying a single provider mapping."""
    return Track(
        item_id=item_id,
        provider=instance_id,
        name=item_id,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=domain, provider_instance=instance_id)
        },
    )


def _music_provider(
    delegate: AudioSource | None,
    *,
    requires_delegate: bool = True,
    instance_id: str = "spotify--a",
) -> MagicMock:
    """Build a music provider mock with the given delegate answer."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance_id
    provider.name = "Spotify"
    provider.get_playback_delegate = AsyncMock(return_value=delegate)
    provider.play_on_delegate = AsyncMock()
    provider.playback_requires_delegate = requires_delegate
    return provider


def _controller(providers: dict[str, Any]) -> PlayerQueuesController:
    """Build a bare controller resolving provider instances from the given mapping."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl.mass = MagicMock()
    ctrl.mass.get_provider = Mock(
        side_effect=lambda instance, *_args, **_kwargs: providers.get(instance)
    )
    return ctrl


async def test_full_delegation_play_swaps_batch_for_the_session() -> None:
    """A fully delegated PLAY hands the items to the provider and plays the session."""
    delegate = _delegate_source()
    provider = _music_provider(delegate)
    ctrl = _controller({"spotify--a": provider})
    items = [_track("t1", "spotify--a", "spotify"), _track("t2", "spotify--a", "spotify")]
    context = Album(
        item_id="alb1",
        provider="spotify--a",
        name="Album",
        provider_mappings={
            ProviderMapping(
                item_id="alb1", provider_domain="spotify", provider_instance="spotify--a"
            )
        },
    )

    result = await ctrl._apply_playback_delegation(
        QUEUE_ID, list(items), QueueOption.PLAY, context, None
    )

    assert result == [delegate]
    provider.play_on_delegate.assert_awaited_once_with(
        delegate, items, QueueOption.PLAY, QUEUE_ID, context=context, start_item=None
    )


@pytest.mark.parametrize("option", [QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT])
async def test_add_family_is_handled_entirely_session_side(option: QueueOption) -> None:
    """A fully delegated ADD/NEXT/REPLACE_NEXT enqueues session-side and loads nothing."""
    delegate = _delegate_source()
    provider = _music_provider(delegate)
    ctrl = _controller({"spotify--a": provider})
    items = [_track("t1", "spotify--a", "spotify")]

    result = await ctrl._apply_playback_delegation(QUEUE_ID, list(items), option, None, None)

    assert result == []
    provider.play_on_delegate.assert_awaited_once_with(
        delegate, items, option, QUEUE_ID, context=None, start_item=None
    )


async def test_add_family_gates_on_enqueueable_media_types() -> None:
    """A session that cannot enqueue is no delegate for the ADD family."""
    delegate = _delegate_source()
    assert delegate.queue_capabilities is not None
    delegate.queue_capabilities.enqueueable_media_types = []
    provider = _music_provider(delegate)
    ctrl = _controller({"spotify--a": provider})
    items = [_track("t1", "spotify--a", "spotify")]

    result = await ctrl._apply_playback_delegation(
        QUEUE_ID, list(items), QueueOption.ADD, None, None
    )

    # not delegatable: the batch passes through so the provider's own (actionable)
    # error surfaces at stream time instead of a silent drop here
    provider.play_on_delegate.assert_not_awaited()
    assert result == items


async def test_play_gates_on_playable_media_types() -> None:
    """Items outside the session's playable media types do not delegate."""
    delegate = _delegate_source()
    assert delegate.queue_capabilities is not None
    delegate.queue_capabilities.playable_media_types = [MediaType.ALBUM]
    provider = _music_provider(delegate, requires_delegate=False)
    ctrl = _controller({"spotify--a": provider})
    items = [_track("t1", "spotify--a", "spotify")]

    result = await ctrl._apply_playback_delegation(
        QUEUE_ID, list(items), QueueOption.PLAY, None, None
    )

    assert result == items
    provider.play_on_delegate.assert_not_awaited()


async def test_mixed_batch_drops_only_delegate_only_items() -> None:
    """Mixed content plays the streamable items and drops the delegate-only ones."""
    provider = _music_provider(_delegate_source())
    other = _music_provider(None, requires_delegate=False, instance_id="tidal--a")
    ctrl = _controller({"spotify--a": provider, "tidal--a": other})
    spotify_track = _track("t1", "spotify--a", "spotify")
    tidal_track = _track("t2", "tidal--a", "tidal")

    result = await ctrl._apply_playback_delegation(
        QUEUE_ID, [spotify_track, tidal_track], QueueOption.PLAY, None, None
    )

    assert result == [tidal_track]
    provider.play_on_delegate.assert_not_awaited()
    cast("MagicMock", ctrl.logger).warning.assert_called_once()


async def test_mixed_batch_keeps_items_of_a_normally_streaming_provider() -> None:
    """With an opportunistic delegate (librespot mode) a mixed batch plays fully normally."""
    provider = _music_provider(_delegate_source(), requires_delegate=False)
    other = _music_provider(None, requires_delegate=False, instance_id="tidal--a")
    ctrl = _controller({"spotify--a": provider, "tidal--a": other})
    spotify_track = _track("t1", "spotify--a", "spotify")
    tidal_track = _track("t2", "tidal--a", "tidal")

    result = await ctrl._apply_playback_delegation(
        QUEUE_ID, [spotify_track, tidal_track], QueueOption.PLAY, None, None
    )

    assert result == [spotify_track, tidal_track]
    provider.play_on_delegate.assert_not_awaited()


async def test_conflicting_delegates_raise_when_nothing_plays_normally() -> None:
    """Two accounts' items delegating to different sessions cannot be mixed."""
    provider_a = _music_provider(_delegate_source("spotify_connect--a"))
    provider_b = _music_provider(_delegate_source("spotify_connect--b"), instance_id="spotify--b")
    ctrl = _controller({"spotify--a": provider_a, "spotify--b": provider_b})
    items: list[MediaItemType] = [
        _track("t1", "spotify--a", "spotify"),
        _track("t2", "spotify--b", "spotify"),
    ]

    with pytest.raises(MediaNotFoundError):
        await ctrl._apply_playback_delegation(QUEUE_ID, items, QueueOption.PLAY, None, None)


async def test_no_delegates_leaves_the_batch_untouched() -> None:
    """Without any delegate the batch passes through unchanged."""
    provider = _music_provider(None, requires_delegate=False)
    ctrl = _controller({"spotify--a": provider})
    items = [_track("t1", "spotify--a", "spotify")]

    result = await ctrl._apply_playback_delegation(
        QUEUE_ID, list(items), QueueOption.PLAY, None, None
    )

    assert result == items


async def test_delegate_lookup_error_is_treated_as_no_delegate() -> None:
    """A failing delegate lookup logs a warning and plays normally."""
    provider = _music_provider(None, requires_delegate=False)
    provider.get_playback_delegate = AsyncMock(side_effect=RuntimeError("boom"))
    ctrl = _controller({"spotify--a": provider})
    items = [_track("t1", "spotify--a", "spotify")]

    result = await ctrl._apply_playback_delegation(
        QUEUE_ID, list(items), QueueOption.PLAY, None, None
    )

    assert result == items
    cast("MagicMock", ctrl.logger).warning.assert_called_once()


async def test_delegate_answer_is_cached_per_provider() -> None:
    """The delegate lookup runs once per provider instance for the whole batch."""
    delegate = _delegate_source()
    provider = _music_provider(delegate)
    ctrl = _controller({"spotify--a": provider})
    items = [_track(f"t{index}", "spotify--a", "spotify") for index in range(5)]

    await ctrl._apply_playback_delegation(QUEUE_ID, list(items), QueueOption.PLAY, None, None)

    provider.get_playback_delegate.assert_awaited_once_with(QUEUE_ID)
