"""Tests for the shared playback session helper."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import SetupFailedError, UnsupportedFeaturedException

from music_assistant.helpers.shared_playback import SharedPlaybackMode, SharedPlaybackSession

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.sendspin.provider import SendspinProvider


def _create_mock_mass(venue_player: MagicMock | None) -> MagicMock:
    """Create a mock MusicAssistant with a mocked players controller."""
    mass = MagicMock()
    mass.players.get_player.return_value = venue_player
    mass.players.cmd_set_members = AsyncMock()
    return mass


def _create_mock_remote_mass(
    sendspin: MagicMock,
) -> tuple[MagicMock, list[asyncio.Task[object]]]:
    """Create a mock MusicAssistant that tracks created tasks."""
    mass = MagicMock()
    tasks: list[asyncio.Task[object]] = []

    def _create_task(
        target: Coroutine[object, object, object],
        **_kwargs: object,
    ) -> asyncio.Task[object]:
        task = asyncio.create_task(target)
        tasks.append(task)
        return task

    mass.get_provider.return_value = sendspin
    mass.create_task.side_effect = _create_task
    return mass, tasks


def _create_venue_player(
    *,
    can_group_with: set[str] | None = None,
    group_members: list[str] | None = None,
    supports_set_members: bool = True,
) -> MagicMock:
    """Create a mock venue player with the given grouping capabilities."""
    player = MagicMock()
    player.state.available = True
    player.state.supported_features = {PlayerFeature.SET_MEMBERS} if supports_set_members else set()
    player.state.can_group_with = can_group_with or set()
    player.state.group_members = group_members or []
    return player


# ==================== VENUE mode ====================


async def test_create_venue_session() -> None:
    """A venue session exposes the venue player's player/queue id."""
    mass = _create_mock_mass(_create_venue_player())

    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    assert session.mode == SharedPlaybackMode.VENUE
    assert session.player_id == "venue_player"
    assert session.queue_id == "venue_player"


async def test_create_venue_unknown_player() -> None:
    """Creating a venue session for an unknown player raises."""
    mass = _create_mock_mass(None)

    with pytest.raises(SetupFailedError):
        await SharedPlaybackSession.create_venue(mass, "unknown_player")


async def test_venue_can_listen_in_feature_detection() -> None:
    """Listen-in is only possible when the venue player can group with the web player."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"})
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    assert session.can_listen_in("web_player_1") is True
    assert session.can_listen_in("web_player_2") is False


async def test_venue_can_listen_in_already_grouped() -> None:
    """A web player that is already a group member can (still) listen in."""
    venue_player = _create_venue_player(group_members=["web_player_1"])
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    assert session.can_listen_in("web_player_1") is True


async def test_venue_can_listen_in_no_set_members() -> None:
    """Listen-in is not possible when the venue player does not support grouping."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"}, supports_set_members=False)
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    assert session.can_listen_in("web_player_1") is False


async def test_venue_add_and_remove_guest_listener() -> None:
    """Guest listeners are attached/detached via the players controller."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"})
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    await session.add_guest_listener("web_player_1")
    mass.players.cmd_set_members.assert_awaited_with(
        "venue_player", player_ids_to_add=["web_player_1"]
    )

    await session.remove_guest_listener("web_player_1")
    mass.players.cmd_set_members.assert_awaited_with(
        "venue_player", player_ids_to_remove=["web_player_1"]
    )


async def test_venue_add_guest_listener_unsupported() -> None:
    """Attaching an incompatible web player raises."""
    venue_player = _create_venue_player(can_group_with=set())
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    with pytest.raises(UnsupportedFeaturedException):
        await session.add_guest_listener("web_player_1")
    mass.players.cmd_set_members.assert_not_awaited()


async def test_venue_close_detaches_only_tracked_listeners() -> None:
    """Closing a venue session only detaches the guest listeners it added."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"})
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")
    await session.add_guest_listener("web_player_1")

    await session.close()

    mass.players.cmd_set_members.assert_awaited_with(
        "venue_player", player_ids_to_remove=["web_player_1"]
    )


async def test_venue_close_without_listeners() -> None:
    """Closing a venue session without listeners never touches the venue player."""
    mass = _create_mock_mass(_create_venue_player())
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    await session.close()

    mass.players.cmd_set_members.assert_not_awaited()


# ==================== REMOTE mode ====================


async def test_create_remote_session(mass: MusicAssistant) -> None:
    """A remote session creates a hidden virtual player that owns a queue."""
    sendspin = cast("SendspinProvider | None", mass.get_provider("sendspin"))
    assert sendspin is not None

    session = await SharedPlaybackSession.create_remote(
        mass, owner_instance_id=sendspin.instance_id, display_name="Test Party"
    )

    assert session.mode == SharedPlaybackMode.REMOTE
    player = mass.players.get_player(session.player_id)
    assert player is not None
    assert player.hidden_by_default is True
    assert mass.player_queues.get(session.queue_id) is not None
    # listen-in is only possible for real, groupable sendspin players
    guest_player_id = await sendspin.create_virtual_player(
        owner_instance_id=sendspin.instance_id, display_name="Guest"
    )
    # refresh the host player's calculated state (normally debounced)
    player.update_state(signal_event=False)
    assert session.can_listen_in(guest_player_id) is True
    assert session.can_listen_in("nonexistent_player") is False
    await sendspin.remove_virtual_player(guest_player_id)

    await session.close()
    assert mass.players.get_player(session.player_id) is None
    # with the virtual host player gone, listen-in is no longer possible
    assert session.can_listen_in(guest_player_id) is False


async def test_create_remote_session_deterministic_id(mass: MusicAssistant) -> None:
    """Re-creating a remote session with the same session_id yields the same player."""
    sendspin = mass.get_provider("sendspin")
    assert sendspin is not None

    session = await SharedPlaybackSession.create_remote(
        mass,
        owner_instance_id=sendspin.instance_id,
        display_name="Test Party",
        session_id="my_party",
    )
    player_id = session.player_id
    await session.close()

    session = await SharedPlaybackSession.create_remote(
        mass,
        owner_instance_id=sendspin.instance_id,
        display_name="Test Party",
        session_id="my_party",
    )
    assert session.player_id == player_id
    await session.close()


async def test_create_remote_session_no_sendspin() -> None:
    """Creating a remote session without the Sendspin provider raises."""
    mass = MagicMock()
    mass.get_provider.return_value = None

    with pytest.raises(SetupFailedError):
        await SharedPlaybackSession.create_remote(
            mass, owner_instance_id="some_plugin", display_name="Test Party"
        )


async def test_cancelled_remote_creation_cleans_up_virtual_player() -> None:
    """Cancel promptly and remove the virtual player after creation finishes."""
    sendspin = MagicMock()
    mass, tasks = _create_mock_remote_mass(sendspin)
    creation_started = asyncio.Event()
    allow_creation = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def _create_virtual_player(**_kwargs: object) -> str:
        creation_started.set()
        await allow_creation.wait()
        return "virtual-player"

    async def _remove_virtual_player(_player_id: str, **_kwargs: object) -> None:
        cleanup_finished.set()

    sendspin.create_virtual_player = AsyncMock(side_effect=_create_virtual_player)
    sendspin.is_virtual_player.return_value = True
    sendspin.remove_virtual_player = AsyncMock(side_effect=_remove_virtual_player)

    session_task = asyncio.create_task(
        SharedPlaybackSession.create_remote(
            mass,
            owner_instance_id="plugin--test",
            display_name="Test Party",
            session_id="test",
        )
    )
    await creation_started.wait()
    session_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await session_task
    assert not cleanup_finished.is_set()

    allow_creation.set()
    await cleanup_finished.wait()
    await asyncio.gather(*tasks)

    sendspin.remove_virtual_player.assert_awaited_once_with("virtual-player")
    assert all(task.done() for task in tasks)


async def test_cancelled_remote_creation_failure_is_observed() -> None:
    """Observe a creation error after the caller has already been cancelled."""
    sendspin = MagicMock()
    mass, tasks = _create_mock_remote_mass(sendspin)
    creation_started = asyncio.Event()
    allow_creation = asyncio.Event()

    async def _create_virtual_player(**_kwargs: object) -> str:
        creation_started.set()
        await allow_creation.wait()
        raise RuntimeError("creation failed")

    sendspin.create_virtual_player = AsyncMock(side_effect=_create_virtual_player)
    sendspin.remove_virtual_player = AsyncMock()

    with patch("music_assistant.helpers.shared_playback.LOGGER") as logger:
        session_task = asyncio.create_task(
            SharedPlaybackSession.create_remote(
                mass,
                owner_instance_id="plugin--test",
                display_name="Test Party",
            )
        )
        await creation_started.wait()
        session_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session_task

        allow_creation.set()
        await tasks[1]

    logger.debug.assert_called_once()
    sendspin.remove_virtual_player.assert_not_awaited()
    assert all(task.done() for task in tasks)


async def test_cancelled_remote_creation_timeout_cancels_task() -> None:
    """Bound cleanup when virtual-player creation does not finish."""
    sendspin = MagicMock()
    mass, tasks = _create_mock_remote_mass(sendspin)
    creation_started = asyncio.Event()
    creation_cancelled = asyncio.Event()
    never_finish = asyncio.Event()

    async def _create_virtual_player(**_kwargs: object) -> str:
        creation_started.set()
        try:
            await never_finish.wait()
        finally:
            creation_cancelled.set()
        return "virtual-player"

    sendspin.create_virtual_player = AsyncMock(side_effect=_create_virtual_player)

    with patch(
        "music_assistant.helpers.shared_playback.REMOTE_CREATION_CLEANUP_TIMEOUT",
        0.01,
    ):
        session_task = asyncio.create_task(
            SharedPlaybackSession.create_remote(
                mass,
                owner_instance_id="plugin--test",
                display_name="Test Party",
            )
        )
        await creation_started.wait()
        session_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session_task

        await creation_cancelled.wait()
        await tasks[1]

    assert tasks[0].cancelled()
    assert all(task.done() for task in tasks)


async def test_cancelled_remote_creation_cleans_up_late_success() -> None:
    """Remove a virtual player returned after bounded creation cleanup ends."""
    sendspin = MagicMock()
    mass, tasks = _create_mock_remote_mass(sendspin)
    creation_started = asyncio.Event()
    cancellation_received = asyncio.Event()
    allow_late_success = asyncio.Event()
    cleanup_finished = asyncio.Event()
    never_finish = asyncio.Event()

    async def _create_virtual_player(**_kwargs: object) -> str:
        creation_started.set()
        try:
            await never_finish.wait()
        except asyncio.CancelledError:
            cancellation_received.set()
            await allow_late_success.wait()
        return "virtual-player"

    async def _remove_virtual_player(_player_id: str, **_kwargs: object) -> None:
        cleanup_finished.set()

    sendspin.create_virtual_player = AsyncMock(side_effect=_create_virtual_player)
    sendspin.is_virtual_player.return_value = True
    sendspin.remove_virtual_player = AsyncMock(side_effect=_remove_virtual_player)

    with patch(
        "music_assistant.helpers.shared_playback.REMOTE_CREATION_CLEANUP_TIMEOUT",
        0.01,
    ):
        session_task = asyncio.create_task(
            SharedPlaybackSession.create_remote(
                mass,
                owner_instance_id="plugin--test",
                display_name="Test Party",
            )
        )
        await creation_started.wait()
        session_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session_task

        await cancellation_received.wait()
        await tasks[1]
        assert not cleanup_finished.is_set()
        allow_late_success.set()
        await cleanup_finished.wait()
        await asyncio.gather(*tasks)

    sendspin.remove_virtual_player.assert_awaited_once_with("virtual-player")
    assert all(task.done() for task in tasks)


async def test_cancelled_remote_creation_retries_cleanup() -> None:
    """Retry cleanup when removing the created virtual player fails temporarily."""
    sendspin = MagicMock()
    mass = MagicMock()
    sendspin.is_virtual_player.return_value = True
    sendspin.remove_virtual_player = AsyncMock(
        side_effect=[RuntimeError("temporary failure"), None]
    )
    cleanup_required = asyncio.get_running_loop().create_future()
    cleanup_required.set_result(True)

    async def _created_player() -> str:
        return "virtual-player"

    creation_task = asyncio.create_task(_created_player())
    with patch(
        "music_assistant.helpers.shared_playback.REMOTE_REMOVAL_CLEANUP_DELAYS",
        (0.0, 0.0),
    ):
        await SharedPlaybackSession._cleanup_cancelled_remote_creation(
            mass,
            sendspin,
            creation_task,
            cleanup_required,
        )

    assert sendspin.remove_virtual_player.await_count == 2


async def test_cancelled_remote_creation_bounds_hung_removal() -> None:
    """Bound cleanup when virtual-player removal does not finish."""
    sendspin = MagicMock()
    mass = MagicMock()
    removal_started = asyncio.Event()
    removal_cancelled = asyncio.Event()
    never_finish = asyncio.Event()

    async def _remove_virtual_player(_player_id: str, **_kwargs: object) -> None:
        removal_started.set()
        try:
            await never_finish.wait()
        finally:
            removal_cancelled.set()

    sendspin.is_virtual_player.return_value = True
    sendspin.remove_virtual_player = AsyncMock(side_effect=_remove_virtual_player)
    cleanup_required = asyncio.get_running_loop().create_future()
    cleanup_required.set_result(True)

    async def _created_player() -> str:
        return "virtual-player"

    creation_task = asyncio.create_task(_created_player())
    with (
        patch(
            "music_assistant.helpers.shared_playback.REMOTE_REMOVAL_CLEANUP_DELAYS",
            (0.0,),
        ),
        patch(
            "music_assistant.helpers.shared_playback.REMOTE_REMOVAL_CLEANUP_TIMEOUT",
            0.01,
        ),
        patch("music_assistant.helpers.shared_playback.LOGGER") as logger,
    ):
        await SharedPlaybackSession._cleanup_cancelled_remote_creation(
            mass,
            sendspin,
            creation_task,
            cleanup_required,
        )

    assert removal_started.is_set()
    assert removal_cancelled.is_set()
    logger.warning.assert_called_once()


async def test_remote_close_is_idempotent(mass: MusicAssistant) -> None:
    """Closing a remote session twice does not raise."""
    sendspin = mass.get_provider("sendspin")
    assert sendspin is not None

    session = await SharedPlaybackSession.create_remote(
        mass, owner_instance_id=sendspin.instance_id, display_name="Test Party"
    )
    await session.close()
    await session.close()
