"""Tests for the Sendspin virtual player API."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlayerFeature, PlayerType
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PLAYERS
from music_assistant.providers.sendspin.constants import (
    CONF_VIRTUAL_PLAYER_OWNER,
    VIRTUAL_PLAYER_ID_PREFIX,
)
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant


def _get_sendspin_provider(mass: MusicAssistant) -> SendspinProvider:
    """Return the loaded Sendspin provider instance."""
    provider = mass.get_provider("sendspin")
    assert provider is not None
    return cast("SendspinProvider", provider)


async def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait until the given condition callable returns True."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("Condition not met within timeout")


async def test_create_virtual_player(mass: MusicAssistant) -> None:
    """Test creating a virtual player registers a hidden queue-owning player."""
    sendspin = _get_sendspin_provider(mass)
    player_id = await sendspin.create_virtual_player(
        owner_instance_id=sendspin.instance_id,
        display_name="Test Session",
    )
    assert player_id.startswith(VIRTUAL_PLAYER_ID_PREFIX)
    assert sendspin.is_virtual_player(player_id)

    player = mass.players.get_player(player_id)
    assert player is not None
    assert player.type == PlayerType.PLAYER
    assert player.hidden_by_default is True
    assert player.private is True
    assert player.expose_to_ha_by_default is False
    assert PlayerFeature.VOLUME_SET not in player.supported_features
    assert PlayerFeature.VOLUME_MUTE not in player.supported_features
    assert PlayerFeature.SET_MEMBERS in player.supported_features
    assert mass.player_queues.get(player_id) is not None
    # the owner marker must be persisted for orphan sweeps
    assert (
        mass.config.get_raw_player_config_value(player_id, CONF_VIRTUAL_PLAYER_OWNER)
        == sendspin.instance_id
    )


async def test_create_virtual_player_custom_id(mass: MusicAssistant) -> None:
    """Test creating a virtual player with a caller-supplied id."""
    sendspin = _get_sendspin_provider(mass)
    player_id = await sendspin.create_virtual_player(
        owner_instance_id=sendspin.instance_id,
        display_name="Test Session",
        player_id="my_session",
    )
    assert player_id == f"{VIRTUAL_PLAYER_ID_PREFIX}my_session"
    assert mass.players.get_player(player_id) is not None


async def test_create_virtual_player_invalid_id(mass: MusicAssistant) -> None:
    """Test that a player_id with unsafe characters is rejected."""
    sendspin = _get_sendspin_provider(mass)
    with pytest.raises(SetupFailedError, match="Invalid player_id"):
        await sendspin.create_virtual_player(
            owner_instance_id=sendspin.instance_id,
            display_name="Test Session",
            player_id="my/session",
        )


async def test_create_virtual_player_duplicate(mass: MusicAssistant) -> None:
    """Test that creating a duplicate virtual player raises."""
    sendspin = _get_sendspin_provider(mass)
    player_id = await sendspin.create_virtual_player(
        owner_instance_id=sendspin.instance_id,
        display_name="Test Session",
        player_id="my_session",
    )
    with pytest.raises(SetupFailedError):
        await sendspin.create_virtual_player(
            owner_instance_id=sendspin.instance_id,
            display_name="Test Session",
            player_id=player_id,
        )


async def test_create_virtual_player_cancellation_cleans_partial_player() -> None:
    """Test cancellation rolls back a partially-created virtual player."""
    sendspin = SendspinProvider.__new__(SendspinProvider)
    sendspin.mass = MagicMock()
    sendspin.server_api = MagicMock()
    sendspin.logger = MagicMock()
    sendspin._virtual_players = {}
    owner = MagicMock(instance_id="owner--test")
    sendspin.mass.get_provider.return_value = owner
    client_registered = asyncio.Event()
    creation_started = asyncio.Event()
    never_finish = asyncio.Event()
    client = MagicMock()

    def _register_virtual_player_client(_player_id: str, _display_name: str) -> None:
        client_registered.set()

    async def _wait_for_virtual_player(_player_id: str) -> None:
        creation_started.set()
        await never_finish.wait()

    sendspin.server_api.get_client.side_effect = lambda _player_id: (
        client if client_registered.is_set() else None
    )
    sendspin.server_api.remove_client = AsyncMock()
    sendspin.mass.players.unregister = AsyncMock()

    with (
        patch.object(
            sendspin,
            "_get_virtual_player_config_owner",
            return_value=None,
        ),
        patch.object(
            sendspin,
            "_register_virtual_player_client",
            side_effect=_register_virtual_player_client,
        ),
        patch.object(
            sendspin,
            "_wait_for_virtual_player",
            new=AsyncMock(side_effect=_wait_for_virtual_player),
        ),
    ):
        creation_task = asyncio.create_task(
            sendspin.create_virtual_player(
                owner_instance_id=owner.instance_id,
                display_name="Test Session",
                player_id="cancelled",
            )
        )
        await creation_started.wait()
        creation_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await creation_task

    player_id = f"{VIRTUAL_PLAYER_ID_PREFIX}cancelled"
    assert not sendspin.is_virtual_player(player_id)
    sendspin.mass.players.unregister.assert_awaited_once_with(player_id, permanent=True)
    sendspin.server_api.remove_client.assert_awaited_once_with(player_id)
    sendspin.mass.players.delete_player_config.assert_called_once_with(player_id)


async def test_create_virtual_player_owner_not_loaded(mass: MusicAssistant) -> None:
    """Test that creating a virtual player for an unknown owner raises."""
    sendspin = _get_sendspin_provider(mass)
    with pytest.raises(SetupFailedError):
        await sendspin.create_virtual_player(
            owner_instance_id="nonexistent_provider",
            display_name="Test Session",
        )


async def test_create_virtual_player_owned_by_other_provider(mass: MusicAssistant) -> None:
    """Test that a persisted virtual player id can not be claimed by another owner."""
    sendspin = _get_sendspin_provider(mass)
    player_id = f"{VIRTUAL_PLAYER_ID_PREFIX}claimed"
    mass.config.set(
        f"{CONF_PLAYERS}/{player_id}",
        {
            "player_id": player_id,
            "provider": sendspin.instance_id,
            "values": {CONF_VIRTUAL_PLAYER_OWNER: "some_other_provider"},
        },
    )
    with pytest.raises(SetupFailedError, match="owned by"):
        await sendspin.create_virtual_player(
            owner_instance_id=sendspin.instance_id,
            display_name="Test Session",
            player_id=player_id,
        )


async def test_remove_virtual_player(mass: MusicAssistant) -> None:
    """Test removing a virtual player cleans up player, client and config."""
    sendspin = _get_sendspin_provider(mass)
    player_id = await sendspin.create_virtual_player(
        owner_instance_id=sendspin.instance_id,
        display_name="Test Session",
    )
    await sendspin.remove_virtual_player(player_id)
    assert not sendspin.is_virtual_player(player_id)
    assert mass.players.get_player(player_id) is None
    assert sendspin.server_api.get_client(player_id) is None
    assert mass.config.get(f"{CONF_PLAYERS}/{player_id}") is None


async def test_remove_virtual_player_retries_after_partial_failure() -> None:
    """Retain virtual-player ownership until removal completes successfully."""
    sendspin = SendspinProvider.__new__(SendspinProvider)
    sendspin.mass = MagicMock()
    sendspin.server_api = MagicMock()
    sendspin.logger = MagicMock()
    player_id = f"{VIRTUAL_PLAYER_ID_PREFIX}retry"
    sendspin._virtual_players = {player_id: "owner--test"}
    sendspin.mass.players.unregister = AsyncMock()
    sendspin.server_api.get_client.return_value = MagicMock()
    sendspin.server_api.remove_client = AsyncMock(
        side_effect=[RuntimeError("client removal failed"), None]
    )

    with pytest.raises(RuntimeError, match="client removal failed"):
        await sendspin.remove_virtual_player(player_id)

    assert sendspin.is_virtual_player(player_id)

    await sendspin.remove_virtual_player(player_id)

    assert not sendspin.is_virtual_player(player_id)
    assert sendspin.mass.players.unregister.await_count == 2
    assert sendspin.server_api.remove_client.await_count == 2
    sendspin.mass.players.delete_player_config.assert_called_once_with(player_id)


async def test_cleanup_failed_creation_awaits_a_slow_teardown() -> None:
    """Test that a slow teardown is awaited to completion instead of being retried."""
    sendspin = SendspinProvider.__new__(SendspinProvider)
    sendspin.mass = MagicMock()
    sendspin.server_api = MagicMock()
    sendspin.logger = MagicMock()
    player_id = f"{VIRTUAL_PLAYER_ID_PREFIX}slow"
    sendspin._virtual_players = {player_id: "owner--test"}
    sendspin.server_api.get_client.return_value = None

    async def _slow_unregister(_player_id: str, **_kwargs: object) -> None:
        # outlasts the 2 second bound this path used to carry
        await asyncio.sleep(2.5)

    sendspin.mass.players.unregister = AsyncMock(side_effect=_slow_unregister)

    await sendspin._cleanup_failed_virtual_player_creation(player_id)

    assert sendspin.mass.players.unregister.await_count == 1
    assert not sendspin.is_virtual_player(player_id)
    sendspin.mass.players.delete_player_config.assert_called_once_with(player_id)
    sendspin.logger.warning.assert_not_called()


async def test_cleanup_failed_creation_retries_a_raised_teardown() -> None:
    """Test that a teardown raising once is retried and then reported as cleaned up."""
    sendspin = SendspinProvider.__new__(SendspinProvider)
    sendspin.mass = MagicMock()
    sendspin.server_api = MagicMock()
    sendspin.logger = MagicMock()
    player_id = f"{VIRTUAL_PLAYER_ID_PREFIX}flaky"
    sendspin._virtual_players = {player_id: "owner--test"}
    sendspin.server_api.get_client.return_value = None
    sendspin.mass.players.unregister = AsyncMock(
        side_effect=[RuntimeError("teardown failed"), None]
    )

    await sendspin._cleanup_failed_virtual_player_creation(player_id)

    assert sendspin.mass.players.unregister.await_count == 2
    assert not sendspin.is_virtual_player(player_id)
    sendspin.logger.warning.assert_not_called()


async def test_cleanup_failed_creation_skips_an_already_removed_player() -> None:
    """Test that cleanup stops immediately when the player is already gone."""
    sendspin = SendspinProvider.__new__(SendspinProvider)
    sendspin.mass = MagicMock()
    sendspin.server_api = MagicMock()
    sendspin.logger = MagicMock()
    player_id = f"{VIRTUAL_PLAYER_ID_PREFIX}gone"
    # already torn down by a racing removal, e.g. the owner-unloaded sweep
    sendspin._virtual_players = {}
    sendspin.mass.players.unregister = AsyncMock()

    async with asyncio.timeout(0.5):
        await sendspin._cleanup_failed_virtual_player_creation(player_id)

    sendspin.mass.players.unregister.assert_not_awaited()
    sendspin.mass.players.delete_player_config.assert_not_called()
    sendspin.logger.warning.assert_not_called()


async def test_remove_virtual_player_rejects_regular_player(mass: MusicAssistant) -> None:
    """Test that removal is refused for players that are not virtual players."""
    sendspin = _get_sendspin_provider(mass)
    with pytest.raises(ValueError, match="not a virtual player"):
        await sendspin.remove_virtual_player("some_regular_player")
    # even a prefixed id is refused when it was never created as virtual player
    with pytest.raises(ValueError, match="not a virtual player"):
        await sendspin.remove_virtual_player(f"{VIRTUAL_PLAYER_ID_PREFIX}unknown")


async def test_virtual_player_removed_on_owner_unload(mass: MusicAssistant) -> None:
    """Test that unloading the owner provider removes its virtual players."""
    await mass.config._create_provider_instance("profiler", {})
    owner = mass.get_provider("profiler")
    assert owner is not None
    await owner.initialized.wait()

    sendspin = _get_sendspin_provider(mass)
    player_id = await sendspin.create_virtual_player(
        owner_instance_id=owner.instance_id,
        display_name="Test Session",
    )
    assert mass.players.get_player(player_id) is not None

    await mass.unload_provider(owner.instance_id)
    await _wait_for(lambda: mass.players.get_player(player_id) is None)
    assert not sendspin.is_virtual_player(player_id)
    assert sendspin.server_api.get_client(player_id) is None


async def test_orphan_virtual_player_config_sweep(mass: MusicAssistant) -> None:
    """Test that stale virtual player configs are swept at provider startup."""
    sendspin = _get_sendspin_provider(mass)
    orphan_id = f"{VIRTUAL_PLAYER_ID_PREFIX}orphan"
    kept_id = f"{VIRTUAL_PLAYER_ID_PREFIX}kept"
    leftover_id = f"{VIRTUAL_PLAYER_ID_PREFIX}leftover"
    for player_id, owner in (
        (orphan_id, "removed_provider"),
        (kept_id, sendspin.instance_id),
    ):
        mass.config.set(
            f"{CONF_PLAYERS}/{player_id}",
            {
                "player_id": player_id,
                "provider": sendspin.instance_id,
                "values": {CONF_VIRTUAL_PLAYER_OWNER: owner},
            },
        )
    # a prefixed config without owner marker must be left alone
    mass.config.set(
        f"{CONF_PLAYERS}/{leftover_id}",
        {"player_id": leftover_id, "provider": sendspin.instance_id, "values": {}},
    )
    # a config of another provider must never be touched by the sweep
    foreign_id = f"{VIRTUAL_PLAYER_ID_PREFIX}foreign"
    mass.config.set(
        f"{CONF_PLAYERS}/{foreign_id}",
        {"player_id": foreign_id, "provider": "other_provider", "values": {}},
    )

    sendspin._remove_orphan_virtual_player_configs()

    assert mass.config.get(f"{CONF_PLAYERS}/{orphan_id}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{leftover_id}") is not None
    assert mass.config.get(f"{CONF_PLAYERS}/{kept_id}") is not None
    assert mass.config.get(f"{CONF_PLAYERS}/{foreign_id}") is not None
