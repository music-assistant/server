"""
Regression tests for the cleanup that runs when a player (or its provider) is removed.

Reproduces the case where a player is first disabled and then removed: disabling
cascades to the linked protocol players, so none of them is registered anymore when
the removal comes in. The leftover (disabled) protocol config is not shown anywhere
and keeps the device from ever registering again, and the leftover queue settings and
queue state are silently inherited by a device that returns under the same player id.

Also covers the mirrored case where the player being removed is not registered (e.g.
its provider was unloaded) while one of its protocol players still is: that protocol
player must be detached from the removed player instead of keeping a dead parent link.

Also covers removing a whole player provider: its unregistered players must have their
DSP/queue settings and persisted queue cache wiped along with their player config,
while players of other providers are left untouched.

Finally, a removed provider or player must also disappear from the per user access
filters, which would otherwise keep pointing at something that no longer exists.
"""

import asyncio
import logging
from collections.abc import Callable, Generator
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import (
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
    ProviderType,
)

from music_assistant.constants import (
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PROTOCOL_PARENT_ID,
    CONF_PROVIDERS,
)
from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
)
from music_assistant.helpers.json import json_loads
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import DeviceInfo, Player

PARENT_ID = "up_esp32"
PROTOCOL_ID = "spb_esp32"
PLAYER_ID = "test_player_1"
# a real, non-builtin player provider with no config entries of its own, so a raw
# provider config can be stored without going through the setup flow; it is
# single-instance, so its instance id equals its domain
PLAYER_PROVIDER_DOMAIN = "dlna"
OTHER_PROVIDER_INSTANCE_ID = "other_provider"


class StubProtocolPlayer:
    """Minimal stand-in for a registered protocol player."""

    def __init__(self, parent_id: str | None) -> None:
        """Initialize the stub with the given (live) protocol parent."""
        self.player_id = PROTOCOL_ID
        # a registered player is looked up and polled by the running server,
        # so carry the state fields those paths read
        self.state = SimpleNamespace(
            type=PlayerType.PROTOCOL,
            playback_state=PlaybackState.IDLE,
            available=True,
            enabled=True,
        )
        self.needs_poll = False
        self.protocol_parent_id = parent_id
        self.refreshed = False
        self.provider = SimpleNamespace(instance_id="sendspin")

    def set_protocol_parent_id(self, parent_id: str | None) -> None:
        """Set the live protocol parent."""
        self.protocol_parent_id = parent_id

    def refresh_state(self) -> None:
        """Record that the state was refreshed."""
        self.refreshed = True


@pytest.fixture(name="register_protocol_player")
def register_protocol_player_fixture(
    mass: MusicAssistant,
) -> Generator[Callable[[str | None], StubProtocolPlayer]]:
    """Register a stub protocol player on the player controller for the test."""

    def _register(parent_id: str | None) -> StubProtocolPlayer:
        protocol_player = StubProtocolPlayer(parent_id)
        mass.players._players[PROTOCOL_ID] = protocol_player  # type: ignore[assignment]
        return protocol_player

    yield _register
    mass.players._players.pop(PROTOCOL_ID, None)


def _pop_scheduled_evaluation(mass: MusicAssistant) -> bool:
    """Return True if a protocol evaluation is pending for the protocol player."""
    if handle := mass.players._pending_protocol_evaluations.pop(PROTOCOL_ID, None):
        handle.cancel()
        return True
    return False


def _store_configs(mass: MusicAssistant, enabled: bool) -> None:
    """Store a universal player config with a single linked protocol player."""
    mass.config.set(
        f"{CONF_PLAYERS}/{PARENT_ID}",
        {
            "player_id": PARENT_ID,
            "provider": "universal_player",
            "player_type": "player",
            "enabled": enabled,
            "values": {"linked_protocol_ids": [PROTOCOL_ID]},
        },
    )
    mass.config.set(
        f"{CONF_PLAYERS}/{PROTOCOL_ID}",
        {
            "player_id": PROTOCOL_ID,
            "provider": "sendspin",
            "player_type": "protocol",
            "enabled": enabled,
            "values": {CONF_PROTOCOL_PARENT_ID: PARENT_ID},
        },
    )
    mass.config.set(f"{CONF_PLAYER_DSP}/{PROTOCOL_ID}", {"enabled": True})


def _store_player_config(
    mass: MusicAssistant, player_id: str, enabled: bool = False, provider: str = "test_provider"
) -> None:
    """Store a plain player config with customised queue settings."""
    mass.config.set(
        f"{CONF_PLAYERS}/{player_id}",
        {
            "player_id": player_id,
            "provider": provider,
            "player_type": "player",
            "enabled": enabled,
            "values": {},
        },
    )
    mass.config.set(
        f"{CONF_PLAYER_QUEUES}/{player_id}",
        {"queue_id": player_id, "values": {"crossfade_duration": 9}},
    )


def _store_provider_config(mass: MusicAssistant) -> None:
    """Store a raw config for the player provider under test."""
    mass.config.set(
        f"{CONF_PROVIDERS}/{PLAYER_PROVIDER_DOMAIN}",
        {
            "type": "player",
            "domain": PLAYER_PROVIDER_DOMAIN,
            "instance_id": PLAYER_PROVIDER_DOMAIN,
            "enabled": True,
            "name": "DLNA",
            "values": {},
        },
    )


async def _store_queue_cache(mass: MusicAssistant, player_id: str) -> None:
    """Store cached queue state and items for the given player."""
    for category in (CACHE_CATEGORY_PLAYER_QUEUE_STATE, CACHE_CATEGORY_PLAYER_QUEUE_ITEMS):
        await mass.cache.set(
            key=player_id,
            data={"queue_id": player_id},
            provider="player_queues",
            category=category,
            persistent=True,
        )


async def _get_queue_cache(mass: MusicAssistant, player_id: str) -> list[object]:
    """Return the cached queue state and items for the given player."""
    return [
        await mass.cache.get(key=player_id, provider="player_queues", category=category)
        for category in (CACHE_CATEGORY_PLAYER_QUEUE_STATE, CACHE_CATEGORY_PLAYER_QUEUE_ITEMS)
    ]


class _TestProvider:
    """Minimal PlayerProvider stand-in that supports removing its players."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the test provider."""
        self.mass = mass
        self.domain = "test_provider"
        self.instance_id = "test_provider"
        self.name = "Test Provider"
        self.available = True
        self.logger = logging.getLogger("test.test_provider")
        self.manifest = MagicMock()
        self.manifest.domain = self.domain
        self.manifest.name = self.name
        self.manifest.type = ProviderType.PLAYER
        self.type = ProviderType.PLAYER

    def check_feature(self, feature: ProviderFeature) -> None:
        """Accept every feature check."""

    async def remove_player(self, player_id: str) -> None:
        """Remove the player, like a real provider does."""
        await self.mass.players.unregister(player_id, permanent=True)

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider (nothing to clean up)."""


class _TestPlayer(Player):
    """Minimal player stand-in."""

    def __init__(self, provider: _TestProvider, player_id: str) -> None:
        """Initialize the test player."""
        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = "Test Player"
        self._attr_type = PlayerType.PLAYER
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
        self._attr_device_info = DeviceInfo(model="Test Model", manufacturer="Test Manufacturer")
        self._cache.clear()
        self.update_state(signal_event=False)

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


async def test_remove_wipes_unregistered_protocol_configs(mass: MusicAssistant) -> None:
    """Removing a disabled player also wipes the config of its linked protocol player."""
    _store_configs(mass, enabled=False)

    await mass.config.remove_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is None
    assert mass.config.get(f"{CONF_PLAYER_DSP}/{PROTOCOL_ID}") is None


async def test_remove_wipes_protocol_configs_with_a_half_broken_link(
    mass: MusicAssistant,
) -> None:
    """A protocol player is wiped by its own parent reference, not by the parent's list."""
    _store_configs(mass, enabled=False)
    mass.config.set(f"{CONF_PLAYERS}/{PARENT_ID}/values/linked_protocol_ids", [])

    await mass.config.remove_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is None


async def test_remove_keeps_reparented_protocol_configs(mass: MusicAssistant) -> None:
    """A protocol player that already moved to another parent keeps its config."""
    _store_configs(mass, enabled=True)
    mass.config.set(f"{CONF_PLAYERS}/{PROTOCOL_ID}/values/{CONF_PROTOCOL_PARENT_ID}", "cast_1")

    mass.players.delete_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is not None


async def test_remove_keeps_registered_protocol_configs(
    mass: MusicAssistant,
    register_protocol_player: Callable[[str | None], StubProtocolPlayer],
) -> None:
    """A protocol player that is still registered keeps its config to be re-parented."""
    _store_configs(mass, enabled=True)
    register_protocol_player(PARENT_ID)

    mass.players.delete_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is not None
    assert mass.config.get(f"{CONF_PLAYER_DSP}/{PROTOCOL_ID}") is not None
    _pop_scheduled_evaluation(mass)


async def test_remove_detaches_registered_protocol_player(
    mass: MusicAssistant,
    register_protocol_player: Callable[[str | None], StubProtocolPlayer],
) -> None:
    """Removing an unregistered parent detaches its still registered protocol player."""
    _store_configs(mass, enabled=True)
    protocol_player = register_protocol_player(PARENT_ID)

    await mass.config.remove_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is not None
    assert protocol_player.protocol_parent_id is None
    assert protocol_player.refreshed
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}/values/{CONF_PROTOCOL_PARENT_ID}") is None
    assert _pop_scheduled_evaluation(mass)


async def test_remove_detaches_protocol_player_waiting_for_its_parent(
    mass: MusicAssistant,
    register_protocol_player: Callable[[str | None], StubProtocolPlayer],
) -> None:
    """A protocol player that only has the parent link in its config is detached too."""
    _store_configs(mass, enabled=True)
    protocol_player = register_protocol_player(None)

    await mass.config.remove_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}/values/{CONF_PROTOCOL_PARENT_ID}") is None
    assert protocol_player.protocol_parent_id is None
    assert _pop_scheduled_evaluation(mass)


async def test_remove_leaves_unrelated_protocol_player_alone(
    mass: MusicAssistant,
    register_protocol_player: Callable[[str | None], StubProtocolPlayer],
) -> None:
    """A protocol player of another parent keeps its link when a player is removed."""
    _store_configs(mass, enabled=True)
    protocol_player = register_protocol_player("cast_1")

    mass.players.delete_player_config(PARENT_ID)

    assert protocol_player.protocol_parent_id == "cast_1"
    assert not _pop_scheduled_evaluation(mass)


async def test_remove_config_wipes_queue_config(mass: MusicAssistant) -> None:
    """Removing the config of an unregistered player also wipes its queue settings."""
    _store_player_config(mass, PLAYER_ID)
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.config.remove_player_config(PLAYER_ID)

    assert mass.config.get(f"{CONF_PLAYER_QUEUES}/{PLAYER_ID}") is None
    assert await _get_queue_cache(mass, PLAYER_ID) == [None, None]


async def test_remove_player_wipes_queue_config(mass: MusicAssistant) -> None:
    """Removing an unregistered player also wipes its queue settings."""
    _store_player_config(mass, PLAYER_ID)
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.players.remove(PLAYER_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is None
    assert mass.config.get(f"{CONF_PLAYER_QUEUES}/{PLAYER_ID}") is None
    assert await _get_queue_cache(mass, PLAYER_ID) == [None, None]


async def test_remove_registered_player_wipes_queue_config(mass: MusicAssistant) -> None:
    """Removing a registered player also wipes its queue settings and state."""
    _store_player_config(mass, PLAYER_ID, enabled=True)
    provider = _TestProvider(mass)
    player = _TestPlayer(provider, PLAYER_ID)
    mass.players._players[PLAYER_ID] = player
    await mass.player_queues.on_player_register(player)
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.config.remove_player_config(PLAYER_ID)

    assert mass.players.get_player(PLAYER_ID) is None
    assert mass.player_queues.get(PLAYER_ID) is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is None
    assert mass.config.get(f"{CONF_PLAYER_QUEUES}/{PLAYER_ID}") is None
    assert await _get_queue_cache(mass, PLAYER_ID) == [None, None]


async def test_remove_wipes_queue_config_of_linked_protocol_player(
    mass: MusicAssistant,
) -> None:
    """The queue settings of a wiped protocol player config go along with it."""
    _store_configs(mass, enabled=False)
    mass.config.set(
        f"{CONF_PLAYER_QUEUES}/{PROTOCOL_ID}",
        {"queue_id": PROTOCOL_ID, "values": {"crossfade_duration": 9}},
    )
    await _store_queue_cache(mass, PROTOCOL_ID)

    await mass.config.remove_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYER_QUEUES}/{PROTOCOL_ID}") is None
    assert await _get_queue_cache(mass, PROTOCOL_ID) == [None, None]


async def test_remove_keeps_queue_config_of_registered_protocol_player(
    mass: MusicAssistant,
    register_protocol_player: Callable[[str | None], StubProtocolPlayer],
) -> None:
    """A protocol player that keeps its config also keeps its queue settings and state."""
    _store_configs(mass, enabled=True)
    mass.config.set(
        f"{CONF_PLAYER_QUEUES}/{PROTOCOL_ID}",
        {"queue_id": PROTOCOL_ID, "values": {"crossfade_duration": 9}},
    )
    await _store_queue_cache(mass, PROTOCOL_ID)
    register_protocol_player(PARENT_ID)

    mass.players.delete_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYER_QUEUES}/{PROTOCOL_ID}") is not None
    assert await _get_queue_cache(mass, PROTOCOL_ID) == [
        {"queue_id": PROTOCOL_ID},
        {"queue_id": PROTOCOL_ID},
    ]
    _pop_scheduled_evaluation(mass)


async def test_remove_provider_config_wipes_unregistered_player_config(
    mass: MusicAssistant,
) -> None:
    """Removing a provider also wipes the DSP/queue settings of its unregistered players."""
    _store_provider_config(mass)
    _store_player_config(mass, PLAYER_ID, provider=PLAYER_PROVIDER_DOMAIN)
    mass.config.set(f"{CONF_PLAYER_DSP}/{PLAYER_ID}", {"enabled": True})
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.config.remove_provider_config(PLAYER_PROVIDER_DOMAIN)

    assert mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is None
    assert mass.config.get(f"{CONF_PLAYER_DSP}/{PLAYER_ID}") is None
    assert mass.config.get(f"{CONF_PLAYER_QUEUES}/{PLAYER_ID}") is None
    assert await _get_queue_cache(mass, PLAYER_ID) == [None, None]


async def test_remove_provider_config_keeps_other_providers_player_config(
    mass: MusicAssistant,
) -> None:
    """A player belonging to a different provider keeps its config untouched."""
    _store_provider_config(mass)
    _store_player_config(mass, PLAYER_ID, provider=OTHER_PROVIDER_INSTANCE_ID)
    mass.config.set(f"{CONF_PLAYER_DSP}/{PLAYER_ID}", {"enabled": True})
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.config.remove_provider_config(PLAYER_PROVIDER_DOMAIN)

    assert mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is not None
    assert mass.config.get(f"{CONF_PLAYER_DSP}/{PLAYER_ID}") is not None
    assert mass.config.get(f"{CONF_PLAYER_QUEUES}/{PLAYER_ID}") is not None
    assert await _get_queue_cache(mass, PLAYER_ID) == [
        {"queue_id": PLAYER_ID},
        {"queue_id": PLAYER_ID},
    ]


async def test_remove_provider_config_wipes_linked_protocol_config(
    mass: MusicAssistant,
) -> None:
    """The config of an unregistered protocol player goes along with its parent's provider."""
    _store_provider_config(mass)
    _store_configs(mass, enabled=False)
    mass.config.set(f"{CONF_PLAYERS}/{PARENT_ID}/provider", PLAYER_PROVIDER_DOMAIN)

    await mass.config.remove_provider_config(PLAYER_PROVIDER_DOMAIN)

    assert mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is None
    assert mass.config.get(f"{CONF_PLAYER_DSP}/{PROTOCOL_ID}") is None


async def test_remove_provider_config_detaches_registered_protocol_player(
    mass: MusicAssistant,
    register_protocol_player: Callable[[str | None], StubProtocolPlayer],
) -> None:
    """A still registered protocol player of another provider is detached, not wiped."""
    _store_provider_config(mass)
    _store_configs(mass, enabled=True)
    mass.config.set(f"{CONF_PLAYERS}/{PARENT_ID}/provider", PLAYER_PROVIDER_DOMAIN)
    protocol_player = register_protocol_player(PARENT_ID)

    await mass.config.remove_provider_config(PLAYER_PROVIDER_DOMAIN)

    assert mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is not None
    assert protocol_player.protocol_parent_id is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}/values/{CONF_PROTOCOL_PARENT_ID}") is None
    assert _pop_scheduled_evaluation(mass)


async def _get_user_filters(mass: MusicAssistant, user_id: str) -> tuple[list[str], list[str]]:
    """Read the raw provider and player filter of the given user."""
    row = await mass.webserver.auth.database.get_row("users", {"user_id": user_id})
    assert row is not None
    return json_loads(row["provider_filter"]), json_loads(row["player_filter"])


async def test_remove_provider_config_strips_user_provider_filter(
    mass: MusicAssistant,
) -> None:
    """Removing a provider also removes it from the access filters of restricted users."""
    _store_provider_config(mass)
    user = await mass.webserver.auth.create_user(
        username="restricted",
        provider_filter=[PLAYER_PROVIDER_DOMAIN, OTHER_PROVIDER_INSTANCE_ID],
    )

    await mass.config.remove_provider_config(PLAYER_PROVIDER_DOMAIN)

    provider_filter, _ = await _get_user_filters(mass, user.user_id)
    assert provider_filter == [OTHER_PROVIDER_INSTANCE_ID]


async def test_remove_player_config_strips_user_player_filter(mass: MusicAssistant) -> None:
    """Removing a player also removes it from the access filters of restricted users."""
    _store_player_config(mass, PLAYER_ID)
    user = await mass.webserver.auth.create_user(
        username="restricted", player_filter=[PLAYER_ID, "other_player"]
    )

    await mass.config.remove_player_config(PLAYER_ID)

    # the filter cleanup is scheduled by the (non-async) config wipe
    deadline = asyncio.get_running_loop().time() + 5.0
    while (await _get_user_filters(mass, user.user_id))[1] != ["other_player"]:
        assert asyncio.get_running_loop().time() < deadline, "player filter was not cleaned up"
        await asyncio.sleep(0.01)
