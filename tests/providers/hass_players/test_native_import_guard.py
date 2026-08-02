"""Tests for blocking the import of HA players that are natively supported in MA."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

from music_assistant_models.enums import IdentifierType

from music_assistant.providers.hass_players.constants import (
    CONF_PLAYERS,
    DISABLED_REASON_NATIVE_DUPLICATE,
    DISABLED_REASON_NATIVE_INTEGRATION,
)
from music_assistant.providers.hass_players.helpers import native_player_macs, normalized_mac
from music_assistant.providers.hass_players.provider import HomeAssistantPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueOption

    from music_assistant.mass import MusicAssistant

PLAY_MEDIA = 512
MAC = "aa:bb:cc:dd:ee:ff"


def _state(entity_id: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": "idle",
        "attributes": {"friendly_name": entity_id, "supported_features": PLAY_MEDIA},
    }


def _entity(entity_id: str, platform: str, device_id: str | None = None) -> dict[str, Any]:
    return {"entity_id": entity_id, "platform": platform, "device_id": device_id}


def _native_player(mac: str | None = MAC, provider_domain: str = "sendspin") -> SimpleNamespace:
    identifiers = {IdentifierType.MAC_ADDRESS: mac} if mac else {}
    return SimpleNamespace(
        provider=SimpleNamespace(domain=provider_domain),
        device_info=SimpleNamespace(identifiers=identifiers),
    )


def _mass(
    entities: list[dict[str, Any]],
    devices: list[dict[str, Any]] | None = None,
    native_players: list[SimpleNamespace] | None = None,
) -> MusicAssistant:
    hass_prov = SimpleNamespace(
        hass=SimpleNamespace(
            connected=True,
            get_entity_registry=AsyncMock(return_value=entities),
            get_device_registry=AsyncMock(return_value=devices or []),
        ),
        get_states=AsyncMock(return_value=[_state(entity["entity_id"]) for entity in entities]),
        logger=logging.getLogger("test.hass"),
    )
    return cast(
        "MusicAssistant",
        SimpleNamespace(
            get_provider=lambda _domain: hass_prov,
            players=native_players or [],
        ),
    )


async def _picker_options(
    mass: MusicAssistant, *, selected_players: list[str] | None = None
) -> list[ConfigValueOption]:
    prov = HomeAssistantPlayerProvider.__new__(HomeAssistantPlayerProvider)
    prov.mass = mass

    # entities already stored under CONF_PLAYERS must stay selectable on a config edit
    def _stored_config_value(key: str, default: Any = None) -> Any:
        return selected_players if key == CONF_PLAYERS else default

    prov.get_config_value = _stored_config_value  # type: ignore[method-assign, assignment]
    entries = await prov.get_config_entries()
    return entries[0].options


async def test_native_integration_entity_is_disabled() -> None:
    """An entity of an integration with a native MA provider can not be newly imported."""
    mass = _mass([_entity("media_player.living_room_tv", "cast")])
    (option,) = await _picker_options(mass)
    assert option.disabled is True
    assert option.disabled_reason == DISABLED_REASON_NATIVE_INTEGRATION


async def test_native_duplicate_device_is_disabled() -> None:
    """An entity whose device is already a native MA player can not be newly imported."""
    mass = _mass(
        [_entity("media_player.kitchen_speaker", "esphome", device_id="dev1")],
        devices=[{"id": "dev1", "connections": [["mac", MAC.upper()]]}],
        native_players=[_native_player()],
    )
    (option,) = await _picker_options(mass)
    assert option.disabled is True
    assert option.disabled_reason == DISABLED_REASON_NATIVE_DUPLICATE


async def test_esphome_entity_without_native_player_is_selectable() -> None:
    """An ESPHome device without a native MA player (no Sendspin) can still be imported."""
    mass = _mass(
        [_entity("media_player.diy_speaker", "esphome", device_id="dev1")],
        devices=[{"id": "dev1", "connections": [["mac", "11:22:33:44:55:66"]]}],
        native_players=[_native_player()],
    )
    (option,) = await _picker_options(mass)
    assert option.disabled is False
    assert option.disabled_reason is None


async def test_already_imported_entity_stays_selectable() -> None:
    """An already imported entity stays selectable so a config edit never drops it."""
    mass = _mass([_entity("media_player.living_room_tv", "cast")])
    (option,) = await _picker_options(mass, selected_players=["media_player.living_room_tv"])
    assert option.disabled is False
    assert option.disabled_reason is None


async def test_regular_entity_is_selectable() -> None:
    """An entity without native MA support is offered as before."""
    mass = _mass([_entity("media_player.kodi_bedroom", "kodi")])
    (option,) = await _picker_options(mass)
    assert option.disabled is False
    assert option.disabled_reason is None


def test_native_player_macs_excludes_own_players() -> None:
    """MACs of players imported through this provider itself are not counted."""
    mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            players=[
                _native_player(),
                _native_player(mac="11:22:33:44:55:66", provider_domain="hass_players"),
                _native_player(mac=None),
            ]
        ),
    )
    assert native_player_macs(mass) == {normalized_mac(MAC)}


def test_normalized_mac() -> None:
    """MAC addresses normalize regardless of separators and casing."""
    assert normalized_mac("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"
    assert normalized_mac("aa-bb-cc-dd-ee-ff") == "aabbccddeeff"
