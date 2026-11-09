"""Tests for how the Plex Connect provider follows its player coming and going."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import EventType
from music_assistant_models.event import MassEvent

from music_assistant.providers.plex_connect import PlexConnectProvider

PLAYER_ID = "player1"


def _make_provider(available: bool) -> tuple[Any, MagicMock]:
    """
    Create a PlexConnectProvider with mocked instance setup and teardown, and its player.

    :param available: Availability of the (registered) connected player.
    """
    mass = MagicMock()
    player = MagicMock()
    player.state.available = available
    mass.players.get_player.side_effect = lambda player_id: (
        player if player_id == PLAYER_ID else None
    )
    setup_data = {"mass_player_id": PLAYER_ID, "plex_provider_id": "plexprov1"}
    mass.config.get = MagicMock(return_value=setup_data)
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    config = MagicMock()
    config.instance_id = "plex_connect_instance_1"
    config.get_value = lambda _key, default=None: default
    config.setup_data = setup_data
    manifest = MagicMock()
    manifest.domain = "plex_connect"

    provider = PlexConnectProvider(mass, manifest, config)
    provider._setup_player_instance = MagicMock()  # type: ignore[method-assign]
    provider._teardown_player_instance = MagicMock()  # type: ignore[method-assign]
    return provider, player


def _event(event: EventType) -> MassEvent:
    """Build an event of the connected player."""
    return MassEvent(event=event, object_id=PLAYER_ID)


async def test_init_waits_for_an_unavailable_player() -> None:
    """A registered but unavailable player is not set up until it comes online."""
    provider, _player = _make_provider(available=False)
    provider._setup_player_instance = AsyncMock()

    await provider.handle_async_init()

    provider._setup_player_instance.assert_not_awaited()
    assert EventType.PLAYER_UPDATED in provider.mass.subscribe.call_args.args[1]


async def test_player_coming_online_starts_plex_connect() -> None:
    """The instance starts once when a registered player turns available."""
    provider, player = _make_provider(available=False)

    provider._on_mass_player_event(_event(EventType.PLAYER_UPDATED))
    provider._setup_player_instance.assert_not_called()

    player.state.available = True
    provider._on_mass_player_event(_event(EventType.PLAYER_UPDATED))
    provider._on_mass_player_event(_event(EventType.PLAYER_UPDATED))
    provider._setup_player_instance.assert_called_once()


async def test_player_added_after_removal_restarts_plex_connect() -> None:
    """A removed player stops the instance and starts it again when it is added back."""
    provider, _player = _make_provider(available=True)
    provider._player_instance = MagicMock()

    provider._on_mass_player_event(_event(EventType.PLAYER_REMOVED))
    provider._teardown_player_instance.assert_called_once()

    provider._player_instance = None
    provider._on_mass_player_event(_event(EventType.PLAYER_ADDED))
    provider._setup_player_instance.assert_called_once()
