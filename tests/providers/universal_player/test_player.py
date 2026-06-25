"""Regression tests for universal player external-source delegation (#5443)."""

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
        return default if default is not None else "auto"

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


def _make_chromecast_player(
    mass: MagicMock,
    player_id: str,
    *,
    active_source: str | None,
    features: set[PlayerFeature],
) -> MagicMock:
    """Return a chromecast-domain protocol player with the given active source and features."""
    player = MagicMock(spec=Player)
    player.player_id = player_id
    player.available = True
    player.active_source = active_source
    player.playback_state = PlaybackState.PLAYING
    player.supported_features = features
    provider = MagicMock()
    provider.domain = "chromecast"
    player.provider = provider
    player.play = AsyncMock()
    player.pause = AsyncMock()
    player.stop = AsyncMock()
    player.next_track = AsyncMock()
    player.previous_track = AsyncMock()
    player.seek = AsyncMock()
    mass.players.get_player = MagicMock(return_value=player)
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
