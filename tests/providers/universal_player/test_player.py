"""Tests for the universal player."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType

from music_assistant.models.player import DeviceInfo, Player
from music_assistant.providers.universal_player.player import UniversalPlayer
from music_assistant.providers.universal_player.provider import UniversalPlayerProvider


def _make_mock_mass() -> MagicMock:
    mass = MagicMock()
    mass.closing = False
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])

    def _get_raw_player_config_value(
        _player_id: str, key: str, default: object | None = None
    ) -> object | None:
        if key == "min_volume":
            return 0
        if key == "max_volume":
            return 100
        return default

    mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw_player_config_value)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    return mass


def _make_universal_provider(mock_mass: MagicMock) -> UniversalPlayerProvider:
    manifest = MagicMock()
    manifest.domain = "universal_player"
    manifest.name = "Universal Player"
    provider = UniversalPlayerProvider.__new__(UniversalPlayerProvider)
    provider.mass = mock_mass
    provider.manifest = manifest
    provider.logger = logging.getLogger("test.universal_player")
    config = MagicMock()
    config.instance_id = "universal_player"
    config.name = None
    provider.config = config
    provider._universal_player_locks = {}
    return provider


def _make_protocol_player(
    player_id: str,
    domain: str,
    *,
    needs_setup: bool = False,
    setup_reason: str | None = None,
) -> MagicMock:
    """Return a reachable protocol player, optionally still awaiting its setup."""
    player = MagicMock(spec=Player)
    player.player_id = player_id
    player.available = True
    player.available_for_playback = not needs_setup
    player.needs_setup = needs_setup
    player.setup_reason = setup_reason
    provider = MagicMock()
    provider.domain = domain
    player.provider = provider
    return player


def _register_players(mass: MagicMock, *players: MagicMock) -> None:
    """Make the given players resolvable by id on the mass mock."""
    registry = {player.player_id: player for player in players}
    mass.players.get_player = MagicMock(side_effect=lambda pid, *_a, **_kw: registry.get(pid))


def _make_chromecast_player(
    mass: MagicMock,
    player_id: str,
    *,
    active_source: str | None,
    features: set[PlayerFeature],
) -> MagicMock:
    """Return a chromecast-domain protocol player with the given active source and features."""
    player = _make_protocol_player(player_id, "chromecast")
    player.active_source = active_source
    player.playback_state = PlaybackState.PLAYING
    player.supported_features = features
    player.play = AsyncMock()
    player.pause = AsyncMock()
    player.stop = AsyncMock()
    player.next_track = AsyncMock()
    player.previous_track = AsyncMock()
    player.seek = AsyncMock()
    _register_players(mass, player)
    return player


def _make_universal_player(mass: MagicMock, protocol_player_ids: list[str]) -> UniversalPlayer:
    provider = _make_universal_provider(mass)
    base_cfg = MagicMock()
    base_cfg.name = None
    base_cfg.default_name = "Universal"
    mass.config.get_base_player_config.return_value = base_cfg
    player = UniversalPlayer(
        provider=provider,
        player_id="up_test",
        name="Universal",
        device_info=DeviceInfo(model="Universal Player", manufacturer="Music Assistant"),
        protocol_player_ids=list(protocol_player_ids),
    )
    player._attr_available = True
    player._attr_type = PlayerType.PLAYER
    player._cache.clear()
    player.set_initialized()
    return player


@pytest.fixture
def setup() -> tuple[UniversalPlayer, MagicMock]:
    """Universal player with a chromecast running Spotify Connect."""
    mass = _make_mock_mass()
    chromecast = _make_chromecast_player(
        mass,
        "cc_1",
        active_source="spotify_connect",
        features={
            PlayerFeature.PAUSE,
            PlayerFeature.SEEK,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.POWER,
            PlayerFeature.PLAY_MEDIA,
        },
    )
    universal = _make_universal_player(mass, ["cc_1"])
    return universal, chromecast


def test_supported_features_forwards_only_transport(
    setup: tuple[UniversalPlayer, MagicMock],
) -> None:
    """
    Only transport features are forwarded; volume/mute/power/play_media are not.

    Volume, mute and power resolve to the protocol player via the base Player's
    *_control logic, so the universal player must not advertise them itself.
    """
    universal, _chromecast = setup
    assert universal.supported_features == {
        PlayerFeature.PAUSE,
        PlayerFeature.SEEK,
        PlayerFeature.NEXT_PREVIOUS,
    }


def test_volume_and_mute_not_captured_as_native(
    setup: tuple[UniversalPlayer, MagicMock],
) -> None:
    """With an external source active, the player does not capture native volume/mute control."""
    universal, _chromecast = setup
    assert universal.volume_control != PLAYER_CONTROL_NATIVE
    assert universal.mute_control != PLAYER_CONTROL_NATIVE


async def test_transport_proxied_to_external_source(
    setup: tuple[UniversalPlayer, MagicMock],
) -> None:
    """Transport commands delegate to the active external source protocol player."""
    universal, chromecast = setup
    await universal.pause()
    chromecast.pause.assert_awaited_once_with()


def test_no_features_without_external_source() -> None:
    """Without an active external source the universal player advertises nothing of its own."""
    mass = _make_mock_mass()
    _make_chromecast_player(
        mass,
        "cc_1",
        active_source=None,
        features={PlayerFeature.PAUSE, PlayerFeature.VOLUME_SET},
    )
    universal = _make_universal_player(mass, ["cc_1"])
    assert universal.supported_features == set()


def test_setup_needed_when_only_protocol_awaits_setup() -> None:
    """A wrapper whose only protocol still needs setup reports it, including the reason."""
    mass = _make_mock_mass()
    airplay = _make_protocol_player(
        "ap_1", "airplay", needs_setup=True, setup_reason="pairing_required"
    )
    _register_players(mass, airplay)
    universal = _make_universal_player(mass, ["ap_1"])
    assert universal.available is False
    assert universal.needs_setup is True
    assert universal.setup_reason == "pairing_required"


def test_no_setup_needed_while_another_protocol_is_usable() -> None:
    """A wrapper that can play through another protocol does not ask for setup."""
    mass = _make_mock_mass()
    airplay = _make_protocol_player(
        "ap_1", "airplay", needs_setup=True, setup_reason="pairing_required"
    )
    chromecast = _make_protocol_player("cc_1", "chromecast")
    _register_players(mass, airplay, chromecast)
    universal = _make_universal_player(mass, ["ap_1", "cc_1"])
    assert universal.available is True
    assert universal.needs_setup is False
    assert universal.setup_reason is None
