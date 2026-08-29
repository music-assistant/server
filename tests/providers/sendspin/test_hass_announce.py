"""Tests for the announcement relay via Home Assistant on ESPHome-backed Sendspin players."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import PlayerConfig

    from music_assistant.mass import MusicAssistant

MAC = "aa:bb:cc:dd:ee:ff"
ENTITY_ID = "media_player.kitchen_speaker"


def _player(*, manufacturer: str = "ESPHome", player_id: str = MAC) -> SendspinPlayer:
    player = SendspinPlayer.__new__(SendspinPlayer)
    player._player_id = player_id
    player._cache = {}
    player._attr_supported_features = set()
    player._attr_device_info = DeviceInfo(manufacturer=manufacturer)
    player._attr_name = "hello name"
    player._config = cast("PlayerConfig", SimpleNamespace(name=None, default_name=None))
    player.logger = Mock()
    return player


def _provider(hass_provider: object | None) -> SendspinProvider:
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast(
        "MusicAssistant", SimpleNamespace(get_provider=lambda _domain: hass_provider)
    )
    provider.logger = Mock()
    return provider


def _hass_provider(device_infos: dict[str, dict[str, str | None]]) -> SimpleNamespace:
    return SimpleNamespace(
        available=True,
        get_media_player_device_infos=AsyncMock(return_value=device_infos),
        play_announcement_on_entity=AsyncMock(),
    )


def test_set_hass_announce_entity_adds_feature() -> None:
    """Setting the HA announce entity adds the PLAY_ANNOUNCEMENT feature."""
    player = _player()
    player.set_hass_announce_entity(ENTITY_ID)
    assert player._hass_announce_entity_id == ENTITY_ID
    assert PlayerFeature.PLAY_ANNOUNCEMENT in player.supported_features


def test_clear_hass_announce_entity_removes_feature() -> None:
    """Clearing the HA announce entity removes the PLAY_ANNOUNCEMENT feature."""
    player = _player()
    player.set_hass_announce_entity(ENTITY_ID)
    player.set_hass_announce_entity(None)
    assert player._hass_announce_entity_id is None
    assert PlayerFeature.PLAY_ANNOUNCEMENT not in player.supported_features


async def test_enrichment_applies_name_and_announce_entity() -> None:
    """An ESPHome player picks up its HA name and announce entity."""
    hass = _hass_provider({MAC: {"name": "Kitchen Speaker", "announce_entity_id": ENTITY_ID}})
    player = _player()
    await _provider(hass)._apply_hass_esphome_enrichment([player])
    assert player._attr_name == "Kitchen Speaker"
    assert player._hass_announce_entity_id == ENTITY_ID
    assert PlayerFeature.PLAY_ANNOUNCEMENT in player.supported_features
    hass.get_media_player_device_infos.assert_awaited_once_with([MAC], platform="esphome")


async def test_enrichment_ignores_non_esphome_players() -> None:
    """Players not backed by ESPHome are left alone entirely."""
    hass = _hass_provider({})
    player = _player(manufacturer="Music Assistant")
    await _provider(hass)._apply_hass_esphome_enrichment([player])
    hass.get_media_player_device_infos.assert_not_awaited()
    assert player._attr_name == "hello name"


@pytest.mark.parametrize("hass_provider", [None, SimpleNamespace(available=False)])
async def test_enrichment_clears_announce_entity_without_hass(hass_provider: object) -> None:
    """The announce relay is dropped when the hass plugin is not available."""
    player = _player()
    player.set_hass_announce_entity(ENTITY_ID)
    await _provider(hass_provider)._apply_hass_esphome_enrichment([player])
    assert player._hass_announce_entity_id is None
    assert PlayerFeature.PLAY_ANNOUNCEMENT not in player.supported_features


async def test_enrichment_clears_announce_entity_for_unknown_device() -> None:
    """A device no longer known to HA loses the announce relay but keeps its name."""
    player = _player()
    player.set_hass_announce_entity(ENTITY_ID)
    await _provider(_hass_provider({}))._apply_hass_esphome_enrichment([player])
    assert player._hass_announce_entity_id is None
    assert player._attr_name == "hello name"


async def test_enrichment_keeps_state_on_hass_error() -> None:
    """A transient HA failure keeps the current enrichment instead of dropping it."""
    hass = SimpleNamespace(
        available=True,
        get_media_player_device_infos=AsyncMock(side_effect=RuntimeError("connection lost")),
    )
    player = _player()
    player.set_hass_announce_entity(ENTITY_ID)
    provider = _provider(hass)
    await provider._apply_hass_esphome_enrichment([player])
    assert player._hass_announce_entity_id == ENTITY_ID
    cast("Mock", provider.logger).warning.assert_called_once()


async def test_play_announcement_relays_via_hass() -> None:
    """An announcement is relayed to the HA entity, ignoring the volume level."""
    hass = _hass_provider({})
    player = _player()
    player.mass = cast("MusicAssistant", SimpleNamespace(get_provider=lambda _domain: hass))
    player.set_hass_announce_entity(ENTITY_ID)
    announcement = PlayerMedia(uri="http://mass.local/announcement.mp3", duration=4)
    await player.play_announcement(announcement, volume_level=50)
    hass.play_announcement_on_entity.assert_awaited_once_with(ENTITY_ID, announcement)


@pytest.mark.parametrize("hass_provider", [None, SimpleNamespace(available=False)])
async def test_play_announcement_requires_hass(hass_provider: object) -> None:
    """An announcement fails cleanly when the hass plugin is gone."""
    player = _player()
    player.mass = cast(
        "MusicAssistant", SimpleNamespace(get_provider=lambda _domain: hass_provider)
    )
    player.set_hass_announce_entity(ENTITY_ID)
    with pytest.raises(PlayerCommandFailed):
        await player.play_announcement(PlayerMedia(uri="http://mass.local/announcement.mp3"))


async def test_play_announcement_requires_resolved_entity() -> None:
    """An announcement fails cleanly when no HA entity was resolved."""
    player = _player()
    player.mass = cast(
        "MusicAssistant", SimpleNamespace(get_provider=lambda _domain: _hass_provider({}))
    )
    with pytest.raises(PlayerCommandFailed):
        await player.play_announcement(PlayerMedia(uri="http://mass.local/announcement.mp3"))
