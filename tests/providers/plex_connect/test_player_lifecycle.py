"""Tests for how the Plex Connect provider follows its player coming and going."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import EventType
from music_assistant_models.event import MassEvent

from music_assistant.providers.plex_connect import PlexConnectProvider

PLAYER_ID = "player1"


def _make_player(available: bool) -> MagicMock:
    """Create the connected player."""
    player = MagicMock()
    player.player_id = PLAYER_ID
    player.state.available = available
    return player


def _make_provider(players: dict[str, MagicMock]) -> Any:
    """
    Create a PlexConnectProvider with mocked instance setup and teardown.

    :param players: The registered players; the caller may change it afterwards.
    """
    mass = MagicMock()
    mass.players.get_player.side_effect = players.get
    mass.players.iter_players.side_effect = lambda **_kwargs: [
        player for player in players.values() if player.state.available
    ]
    # setup and teardown run as tasks; close them, their calls are what is checked
    mass.create_task.side_effect = lambda coro, **_kwargs: coro.close()
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
    provider._setup_player_instance = AsyncMock()  # type: ignore[method-assign]
    provider._teardown_player_instance = AsyncMock()  # type: ignore[method-assign]
    return provider


def _event(event: EventType) -> MassEvent:
    """Build an event of the connected player."""
    return MassEvent(event=event, object_id=PLAYER_ID)


async def test_init_waits_for_an_unavailable_player() -> None:
    """A registered but unavailable player is not set up until it comes online."""
    provider = _make_provider({PLAYER_ID: _make_player(available=False)})

    await provider.handle_async_init()

    provider._setup_player_instance.assert_not_called()
    assert EventType.PLAYER_UPDATED in provider.mass.subscribe.call_args.args[1]


async def test_init_sets_up_an_available_player_once() -> None:
    """An available player is set up at init and its later updates start nothing more."""
    provider = _make_provider({PLAYER_ID: _make_player(available=True)})

    await provider.handle_async_init()
    provider._on_mass_player_event(_event(EventType.PLAYER_UPDATED))

    provider._setup_player_instance.assert_called_once()


async def test_player_coming_online_starts_plex_connect() -> None:
    """The instance starts once when a registered player turns available."""
    player = _make_player(available=False)
    provider = _make_provider({PLAYER_ID: player})
    await provider.handle_async_init()

    provider._on_mass_player_event(_event(EventType.PLAYER_UPDATED))
    provider._setup_player_instance.assert_not_called()

    player.state.available = True
    provider._on_mass_player_event(_event(EventType.PLAYER_UPDATED))
    provider._on_mass_player_event(_event(EventType.PLAYER_UPDATED))
    provider._setup_player_instance.assert_called_once()


async def test_player_returning_after_a_drop_before_init_starts_plex_connect() -> None:
    """A player that drops while the provider loads is set up once it comes back."""
    player = _make_player(available=True)
    players = {PLAYER_ID: player}
    provider = _make_provider(players)

    # the player drops while the provider waits for the Plex provider
    del players[PLAYER_ID]
    await provider.handle_async_init()
    provider._setup_player_instance.assert_not_called()

    players[PLAYER_ID] = player
    provider._on_mass_player_event(_event(EventType.PLAYER_ADDED))
    provider._setup_player_instance.assert_called_once()


async def test_player_added_after_removal_restarts_plex_connect() -> None:
    """A removed player stops the instance and starts it again when it is added back."""
    provider = _make_provider({PLAYER_ID: _make_player(available=True)})
    await provider.handle_async_init()
    provider._player_instance = MagicMock()

    provider._on_mass_player_event(_event(EventType.PLAYER_REMOVED))
    provider._teardown_player_instance.assert_called_once()

    provider._player_instance = None
    provider._on_mass_player_event(_event(EventType.PLAYER_ADDED))
    assert provider._setup_player_instance.call_count == 2
