"""
Regression tests for removing a player config with linked protocol players.

Reproduces the case where a player is first disabled and then removed: disabling
cascades to the linked protocol players, so none of them is registered anymore when
the removal comes in. The leftover (disabled) protocol config is not shown anywhere
and keeps the device from ever registering again.

Also covers the mirrored case where the player being removed is not registered (e.g.
its provider was unloaded) while one of its protocol players still is: that protocol
player must be detached from the removed player instead of keeping a dead parent link.
"""

from collections.abc import Callable, Generator
from types import SimpleNamespace

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType

from music_assistant.constants import CONF_PLAYER_DSP, CONF_PLAYERS, CONF_PROTOCOL_PARENT_ID
from music_assistant.mass import MusicAssistant

PARENT_ID = "up_esp32"
PROTOCOL_ID = "spb_esp32"


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
