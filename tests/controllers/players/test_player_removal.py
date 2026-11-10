"""Tests for removing a player, which the players controller does for a player of any provider."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import (
    EventType,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import UnsupportedFeaturedException

from music_assistant.constants import CONF_PLAYER_DSP, CONF_PLAYER_QUEUES, CONF_PLAYERS
from music_assistant.models.player import DeviceInfo, Player
from music_assistant.providers import sync_group, universal_group

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant

PLAYER_ID = "test_player_1"
GROUP_ID = "test_group_1"
PROVIDER = "test_provider"


class _TestProvider:
    """A PlayerProvider stand-in that knows nothing about removing players."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the test provider."""
        self.mass = mass
        self.domain = self.instance_id = PROVIDER
        self.name = "Test Provider"
        self.available = True
        self.logger = logging.getLogger("test.test_provider")
        self.manifest = MagicMock()
        self.manifest.domain = self.domain
        self.manifest.name = self.name
        self.type = self.manifest.type = ProviderType.PLAYER

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider (nothing to clean up)."""


class _TestPlayer(Player):
    """A player without a removal hook of its own."""

    def __init__(
        self, provider: _TestProvider, player_type: PlayerType, player_id: str = PLAYER_ID
    ) -> None:
        """Initialize the test player."""
        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = "Test Player"
        self._attr_type = player_type
        self._attr_available = True
        self._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
        self._attr_device_info = DeviceInfo(model="Test Model", manufacturer="Test")
        self._cache.clear()
        self.update_state(signal_event=False)
        self.unloaded = False

    async def stop(self) -> None:
        """Stop playback - required abstract method."""

    async def on_unload(self) -> None:
        """Record that the player was unloaded."""
        await super().on_unload()
        self.unloaded = True


class _HookedPlayer(_TestPlayer):
    """A player whose provider has something of its own to clean up on removal."""

    def __init__(self, provider: _TestProvider, player_type: PlayerType) -> None:
        """Initialize the test player."""
        super().__init__(provider, player_type)
        # what the hook found when it was called, once per call
        self.hook_calls: list[dict[str, bool]] = []
        self.hook_error: Exception | None = None

    async def on_remove(self) -> None:
        """Record the state of the removal at the time the hook runs."""
        self.hook_calls.append(
            {
                "unloaded": self.unloaded,
                "registered": self.mass.players.get_player(PLAYER_ID) is not None,
                "config": self.mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is not None,
            }
        )
        if self.hook_error:
            raise self.hook_error


async def _register[PlayerT: _TestPlayer](
    mass: MusicAssistant,
    player_class: type[PlayerT],
    player_type: PlayerType = PlayerType.PLAYER,
) -> PlayerT:
    """Put a player in the registry with its config, its DSP and queue settings and a queue."""
    player = player_class(_TestProvider(mass), player_type)
    mass.config.create_default_player_config(PLAYER_ID, PROVIDER, player_type, player.name)
    mass.config.set(f"{CONF_PLAYER_DSP}/{PLAYER_ID}", {"enabled": True})
    mass.config.set(f"{CONF_PLAYER_QUEUES}/{PLAYER_ID}", {"queue_id": PLAYER_ID, "values": {}})
    mass.players._players[PLAYER_ID] = player
    await mass.player_queues.on_player_register(player)
    return player


def _assert_removed(mass: MusicAssistant) -> None:
    """Assert that the player and everything stored for it is gone."""
    assert mass.players.get_player(PLAYER_ID) is None
    assert mass.player_queues.get(PLAYER_ID) is None
    for tree in (CONF_PLAYERS, CONF_PLAYER_DSP, CONF_PLAYER_QUEUES):
        assert mass.config.get(f"{tree}/{PLAYER_ID}") is None


async def test_a_player_without_a_removal_hook_is_removable(mass: MusicAssistant) -> None:
    """A player is removed by the controller, whatever its provider knows about removal."""
    player = await _register(mass, _TestPlayer)
    removed: list[MassEvent] = []
    unsubscribe = mass.subscribe(removed.append, EventType.PLAYER_REMOVED)

    await mass.players.remove(PLAYER_ID)
    await asyncio.sleep(0)
    unsubscribe()

    assert player.unloaded
    _assert_removed(mass)
    assert [event.object_id for event in removed] == [PLAYER_ID]


async def test_the_removal_hook_is_called_once_the_player_is_gone(mass: MusicAssistant) -> None:
    """The hook of the player object runs after the player was unloaded and its config deleted."""
    player = await _register(mass, _HookedPlayer)

    await mass.players.remove(PLAYER_ID)

    assert player.hook_calls == [{"unloaded": True, "registered": False, "config": False}]
    _assert_removed(mass)


class _GroupPlayer(_TestPlayer):
    """A dynamic group that records in which state a member leaves it."""

    def __init__(self, provider: _TestProvider) -> None:
        """Initialize the test group."""
        super().__init__(provider, PlayerType.GROUP, GROUP_ID)
        # whether each member that left was still a registered player at that time
        self.left_while_registered: dict[str, bool] = {}

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Record the members that are taken out of the group."""
        for member_id in player_ids_to_remove or []:
            self.left_while_registered[member_id] = (
                self.mass.players.get_player(member_id) is not None
            )


async def test_a_member_of_an_active_group_leaves_it_before_it_is_unregistered(
    mass: MusicAssistant,
) -> None:
    """A removed player is taken out of its active group while the group can still reach it."""
    player = await _register(mass, _HookedPlayer)
    group = _GroupPlayer(_TestProvider(mass))
    mass.players._players[GROUP_ID] = group
    player.state.active_group = GROUP_ID

    try:
        await mass.players.remove(PLAYER_ID)
    finally:
        mass.players._players.pop(GROUP_ID)

    assert group.left_while_registered == {PLAYER_ID: True}
    assert len(player.hook_calls) == 1
    _assert_removed(mass)


async def test_a_failing_removal_hook_does_not_keep_the_player(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider that fails to clean up its own leaves the player removed all the same."""
    player = await _register(mass, _HookedPlayer)
    player.hook_error = RuntimeError("device unreachable")

    with caplog.at_level(logging.ERROR):
        await mass.players.remove(PLAYER_ID)

    assert len(player.hook_calls) == 1
    _assert_removed(mass)
    assert "Error removing player Test Player" in caplog.text


async def test_the_removal_hook_is_not_called_when_a_player_is_only_unregistered(
    mass: MusicAssistant,
) -> None:
    """A provider that takes its own player away for good gets no removal hook."""
    player = await _register(mass, _HookedPlayer)

    await mass.players.unregister(PLAYER_ID, permanent=True)

    assert player.hook_calls == []
    _assert_removed(mass)


async def test_removing_the_config_of_a_registered_player_removes_the_player(
    mass: MusicAssistant,
) -> None:
    """Removing a player's config is removing the player, also while it is registered."""
    player = await _register(mass, _HookedPlayer)

    await mass.config.remove_player_config(PLAYER_ID)

    assert len(player.hook_calls) == 1
    _assert_removed(mass)


async def test_a_group_player_is_removed_like_any_other_player(mass: MusicAssistant) -> None:
    """The group command removes a group through the same path, hook included."""
    player = await _register(mass, _HookedPlayer, PlayerType.GROUP)

    await mass.players.remove_group_player(PLAYER_ID)

    assert len(player.hook_calls) == 1
    _assert_removed(mass)


@pytest.mark.parametrize("command", ["players/remove", "players/remove_group_player"])
async def test_a_sync_group_is_removed_by_the_controller(
    mass: MusicAssistant, command: str
) -> None:
    """A group the user created goes with either command; its provider has no part in it."""
    group = await mass.players.create_group_player("sync_group", "Group", [], dynamic=True)
    group_id = group.player_id
    assert mass.config.get(f"{CONF_PLAYERS}/{group_id}") is not None

    if command == "players/remove":
        await mass.players.remove(group_id)
    else:
        await mass.players.remove_group_player(group_id)

    assert mass.players.get_player(group_id) is None
    assert mass.config.get(f"{CONF_PLAYERS}/{group_id}") is None


async def test_the_group_command_only_removes_group_players(mass: MusicAssistant) -> None:
    """The group command refuses a player that is no group and leaves it alone."""
    player = await _register(mass, _HookedPlayer)

    with pytest.raises(UnsupportedFeaturedException, match="is not a group player"):
        await mass.players.remove_group_player(PLAYER_ID)

    assert player.hook_calls == []
    assert mass.players.get_player(PLAYER_ID) is player
    assert mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is not None


@pytest.mark.parametrize("module", [sync_group, universal_group])
def test_the_group_providers_still_offer_the_removal_of_groups(module: object) -> None:
    """The providers of the groups a user creates keep advertising their removal."""
    features: set[ProviderFeature] = module.SUPPORTED_FEATURES  # type: ignore[attr-defined]

    assert ProviderFeature.REMOVE_GROUP_PLAYER in features
