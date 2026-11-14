"""Tests for how the Plex Connect provider follows its player coming and going."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import EventType
from music_assistant_models.event import MassEvent

from music_assistant.providers.plex_connect import PlexConnectProvider
from tests.common import MockProvider, attach_mock, mock_player_mass

PLAYER_ID = "player1"


def _make_player(available: bool) -> MagicMock:
    """Create the connected player."""
    player = MagicMock()
    player.player_id = PLAYER_ID
    player.available = available
    return player


def _make_provider(
    players: dict[str, MagicMock],
    mass: MagicMock | None = None,
    player_id: str = PLAYER_ID,
    legacy_ids: dict[str, str] | None = None,
) -> Any:
    """
    Create a PlexConnectProvider with mocked instance setup and teardown.

    :param players: The registered players; the caller may change it afterwards.
    :param mass: A mass whose players controller has players of its own, instead of
        one that serves the given players.
    :param player_id: The player the provider is connected to.
    :param legacy_ids: The ids of merged players, mapped to the player they went into;
        the caller may change it afterwards.
    """
    if mass is None:
        mass = MagicMock()
        mass.players.get_player.side_effect = players.get
        legacy = legacy_ids if legacy_ids is not None else {}
        mass.players.resolve_player_id.side_effect = lambda pid: legacy.get(pid, pid)
        mass.players.iter_players.side_effect = lambda **_kwargs: [
            player for player in players.values() if player.available
        ]
    # setup and teardown run as tasks; close them, their calls are what is checked
    mass.create_task.side_effect = lambda coro, **_kwargs: coro.close()
    setup_data = {"mass_player_id": player_id, "plex_provider_id": "plexprov1"}
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

    player.available = True
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


async def test_a_player_whose_id_is_not_its_protocols_is_followed_by_its_player_id() -> None:
    """A connected player with an id of its own is set up and followed under that id."""
    mass = mock_player_mass()
    player, _protocol = attach_mock(MockProvider("airplay", mass=mass), "ap_1")
    player.update_state()
    provider = _make_provider({}, mass=mass, player_id=player.player_id)

    await provider.handle_async_init()
    provider._setup_player_instance.assert_called_once()

    provider._player_instance = MagicMock()
    provider._on_mass_player_event(MassEvent(event=EventType.PLAYER_REMOVED, object_id="pl_ap_1"))
    provider._teardown_player_instance.assert_called_once()


async def test_plex_connect_follows_its_player_into_the_player_it_is_merged_into() -> None:
    """A merge of the connected player moves the instance to the player it went into."""
    legacy_ids: dict[str, str] = {}
    players = {PLAYER_ID: _make_player(available=True)}
    provider = _make_provider(players, legacy_ids=legacy_ids)
    await provider.handle_async_init()
    restart = provider._restart_player_instance = AsyncMock()

    merged_into = _make_player(available=True)
    merged_into.player_id = "player2"
    players.pop(PLAYER_ID)
    players["player2"] = merged_into
    legacy_ids[PLAYER_ID] = "player2"
    provider._on_mass_player_event(_event(EventType.PLAYER_REMOVED))

    restart.assert_called_once()
    provider._teardown_player_instance.assert_not_called()
    # the events of the player it went into are followed from now on
    provider._player_instance = None
    merged_into.available = False
    provider._on_mass_player_event(MassEvent(event=EventType.PLAYER_UPDATED, object_id="player2"))
    merged_into.available = True
    provider._on_mass_player_event(MassEvent(event=EventType.PLAYER_UPDATED, object_id="player2"))
    assert provider._setup_player_instance.call_count == 2


async def test_restart_sets_up_the_player_it_is_connected_to_now() -> None:
    """The instance is started again on the player the stored one was merged into."""
    merged_into = _make_player(available=True)
    merged_into.player_id = "player2"
    provider = _make_provider({"player2": merged_into}, legacy_ids={PLAYER_ID: "player2"})
    provider._player_instance = MagicMock()

    await provider._restart_player_instance()

    provider._teardown_player_instance.assert_called_once()
    provider._setup_player_instance.assert_called_once()


async def test_instance_plays_on_the_player_the_stored_one_was_merged_into() -> None:
    """The remote control instance gets the id of the player, not the stored legacy id."""
    merged_into = _make_player(available=True)
    merged_into.player_id = "player2"
    merged_into.display_name = "Kitchen"
    provider = _make_provider({"player2": merged_into}, legacy_ids={PLAYER_ID: "player2"})
    del provider._setup_player_instance
    provider._plex_provider = MagicMock()
    provider._allocated_port = 32500

    with patch("music_assistant.providers.plex_connect.PlayerRemoteInstance") as instance:
        instance.return_value.start = AsyncMock()
        await provider._setup_player_instance()

    assert instance.call_args.kwargs["ma_player_id"] == "player2"
    assert provider._instance_player_id == "player2"


async def test_plex_connect_follows_its_player_through_a_chain_of_merges() -> None:
    """The instance moves on when the player it went into is merged into a third one."""
    legacy_ids: dict[str, str] = {}
    players = {PLAYER_ID: _make_player(available=True)}
    provider = _make_provider(players, legacy_ids=legacy_ids)
    await provider.handle_async_init()
    restart = provider._restart_player_instance = AsyncMock()
    legacy_ids[PLAYER_ID] = "player2"
    provider._on_mass_player_event(_event(EventType.PLAYER_REMOVED))
    # the instance runs on the player the configured one went into
    provider._instance_player_id = "player2"

    legacy_ids.update({PLAYER_ID: "player3", "player2": "player3"})
    provider._on_mass_player_event(MassEvent(event=EventType.PLAYER_REMOVED, object_id="player2"))

    assert restart.call_count == 2
