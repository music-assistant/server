"""
Tests for routing player commands to whatever is producing the audio.

A player command applies to what the player is actually playing: a live external
source handles it in its own session, and Music Assistant's queue handles it for
its own items. This replaces the queue-delegation tests, which covered the same
forwarding while a live source was a queue item.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ProviderFeature, RepeatMode, SourceControl
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioSource
from music_assistant_models.media_items.provider_mapping import ProviderMapping

from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.audio_sources import AudioSourceSession
from music_assistant.models.plugin import PluginProvider

PLAYER_ID = "player_1"
PROVIDER_INSTANCE = "spotify_connect--abc"


def _source(
    *,
    can_play_pause: bool = False,
    can_seek: bool = False,
    can_next_previous: bool = False,
) -> AudioSource:
    return AudioSource(
        item_id="main",
        provider=PROVIDER_INSTANCE,
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id="main",
                provider_domain="spotify_connect",
                provider_instance=PROVIDER_INSTANCE,
            )
        },
        can_play_pause=can_play_pause,
        can_seek=can_seek,
        can_next_previous=can_next_previous,
    )


def _controller(source: AudioSource | None) -> tuple[Any, MagicMock]:
    """Build a controller with (or without) a live source on PLAYER_ID."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "INFO"
    controller = PlayerController(mass)
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = PROVIDER_INSTANCE
    provider.supported_features = {ProviderFeature.AUDIO_SOURCE}
    provider.on_source_control = AsyncMock()
    mass.get_provider.return_value = provider
    if source is not None:
        controller._source_sessions[PLAYER_ID] = AudioSourceSession(
            player_id=PLAYER_ID,
            source=source,
            provider_instance_id=PROVIDER_INSTANCE,
        )
    player = MagicMock()
    player.player_id = PLAYER_ID
    player.display_name = "Player 1"
    player.available = True
    player.state.synced_to = None
    player.state.active_group = None
    player.protocol_parent_id = None
    controller.get_player = MagicMock(return_value=player)  # type: ignore[method-assign]
    # the command decorator looks the player up in the registry, not via get_player
    controller._players[PLAYER_ID] = player
    return controller, provider


async def test_a_capable_source_takes_the_command() -> None:
    """The source is asked to seek within its own session."""
    controller, provider = _controller(_source(can_seek=True))

    handled = await controller._forward_to_external_source(
        controller.get_player(PLAYER_ID), SourceControl.SEEK, 42
    )

    assert handled is True
    provider.on_source_control.assert_awaited_once_with("main", SourceControl.SEEK, 42)


async def test_a_source_that_cannot_do_it_refuses_rather_than_forwarding() -> None:
    """A client is told no instead of being left waiting on a command nothing handles."""
    controller, provider = _controller(_source(can_seek=False))

    with pytest.raises(PlayerCommandFailed, match="does not support this action"):
        await controller._forward_to_external_source(
            controller.get_player(PLAYER_ID), SourceControl.SEEK, 42
        )

    provider.on_source_control.assert_not_awaited()


@pytest.mark.parametrize(
    ("action", "flag"),
    [
        (SourceControl.PLAY, "can_play_pause"),
        (SourceControl.PAUSE, "can_play_pause"),
        (SourceControl.NEXT, "can_next_previous"),
        (SourceControl.PREVIOUS, "can_next_previous"),
        (SourceControl.SEEK, "can_seek"),
    ],
)
async def test_each_transport_action_is_gated_on_its_own_flag(
    action: SourceControl, flag: str
) -> None:
    """Every transport action is gated by the capability that describes it."""
    controller, _provider = _controller(_source(**{flag: True}))
    assert await controller._forward_to_external_source(controller.get_player(PLAYER_ID), action)

    controller, _provider = _controller(_source(**{flag: False}))
    with pytest.raises(PlayerCommandFailed):
        await controller._forward_to_external_source(controller.get_player(PLAYER_ID), action)


@pytest.mark.parametrize("action", [SourceControl.SHUFFLE, SourceControl.REPEAT])
async def test_ordering_is_not_gated_by_a_capability_flag(action: SourceControl) -> None:
    """
    Shuffle and repeat go to the session unconditionally.

    Only the session knows whether its content can be reordered, and it refuses in
    its own words — a flag here would second-guess it.
    """
    controller, provider = _controller(_source())

    assert await controller._forward_to_external_source(
        controller.get_player(PLAYER_ID), action, True
    )

    provider.on_source_control.assert_awaited_once_with("main", action, True)


async def test_nothing_is_forwarded_when_no_source_is_playing() -> None:
    """With no live source the caller is told to look elsewhere, not refused."""
    controller, provider = _controller(None)

    handled = await controller._forward_to_external_source(
        controller.get_player(PLAYER_ID), SourceControl.SEEK, 42
    )

    assert handled is False
    provider.on_source_control.assert_not_awaited()


async def test_a_gone_provider_is_not_forwarded_to() -> None:
    """A session whose plugin has unloaded forwards nothing rather than raising."""
    controller, _provider = _controller(_source(can_seek=True))
    controller.mass.get_provider.return_value = None

    assert (
        await controller._forward_to_external_source(
            controller.get_player(PLAYER_ID), SourceControl.SEEK, 42
        )
        is False
    )


async def test_a_group_member_is_playing_its_groups_source() -> None:
    """A member hearing its group's audio resolves to the group's source, not its own."""
    controller, provider = _controller(None)
    group_source = _source(can_seek=True)
    controller._source_sessions["group_1"] = AudioSourceSession(
        player_id="group_1",
        source=group_source,
        provider_instance_id=PROVIDER_INSTANCE,
    )
    member = MagicMock()
    member.player_id = "member_1"
    member.display_name = "Member"
    member.state.synced_to = None
    member.state.active_group = "group_1"
    member.protocol_parent_id = None
    group = MagicMock()
    group.player_id = "group_1"
    group.state.synced_to = None
    group.state.active_group = None
    group.protocol_parent_id = None
    controller.get_player = MagicMock(
        side_effect=lambda pid, *_a, **_k: {"member_1": member, "group_1": group}.get(pid)
    )

    assert await controller._forward_to_external_source(member, SourceControl.SEEK, 7)

    provider.on_source_control.assert_awaited_once_with("main", SourceControl.SEEK, 7)


async def test_shuffle_falls_through_to_the_queue_when_no_source_is_playing() -> None:
    """Without a live source, shuffle is the Music Assistant queue's business."""
    controller, provider = _controller(None)
    controller._get_player_with_redirect = MagicMock(return_value=controller.get_player(PLAYER_ID))
    queue = MagicMock()
    queue.queue_id = PLAYER_ID
    controller.get_active_queue = MagicMock(return_value=queue)
    controller.mass.player_queues.set_shuffle = AsyncMock()

    await controller.cmd_shuffle(PLAYER_ID, shuffle_enabled=True)

    controller.mass.player_queues.set_shuffle.assert_awaited_once_with(PLAYER_ID, True)
    provider.on_source_control.assert_not_awaited()


async def test_repeat_reaches_the_live_source_before_the_queue() -> None:
    """A live source is what is playing, so it gets the command and the queue does not."""
    controller, provider = _controller(_source())
    controller._get_player_with_redirect = MagicMock(return_value=controller.get_player(PLAYER_ID))
    controller.get_active_queue = MagicMock(return_value=MagicMock())
    controller.mass.player_queues.set_repeat = AsyncMock()

    await controller.cmd_repeat(PLAYER_ID, RepeatMode.ALL)

    provider.on_source_control.assert_awaited_once_with(
        "main", SourceControl.REPEAT, RepeatMode.ALL
    )
    controller.mass.player_queues.set_repeat.assert_not_awaited()
