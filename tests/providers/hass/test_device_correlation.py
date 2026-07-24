"""Tests for correlating natively connected devices to their Home Assistant representation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from music_assistant.providers.hass import HomeAssistantProvider

MEDIA_ANNOUNCE = 1048576
MAC = "aa:bb:cc:dd:ee:ff"


def _provider(
    devices: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    states: list[dict[str, Any]],
) -> HomeAssistantProvider:
    provider = HomeAssistantProvider.__new__(HomeAssistantProvider)
    provider.hass = SimpleNamespace(
        get_device_registry=AsyncMock(return_value=devices),
        get_entity_registry=AsyncMock(return_value=entities),
    )
    provider.get_states = AsyncMock(return_value=states)  # type: ignore[method-assign]
    return provider


def _device(device_id: str = "dev1", name_by_user: str | None = None) -> dict[str, Any]:
    return {
        "id": device_id,
        "name": "Kitchen Speaker",
        "name_by_user": name_by_user,
        "connections": [["mac", MAC.upper()]],
    }


def _entity(
    entity_id: str,
    device_id: str = "dev1",
    platform: str = "esphome",
    disabled_by: str | None = None,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "platform": platform,
        "device_id": device_id,
        "disabled_by": disabled_by,
    }


def _state(entity_id: str, supported_features: int) -> dict[str, Any]:
    return {"entity_id": entity_id, "attributes": {"supported_features": supported_features}}


async def test_correlates_name_and_announce_entity() -> None:
    """A device is matched case-insensitively on MAC with its announce-capable entity."""
    provider = _provider(
        [_device(name_by_user="Kitchen")],
        [_entity("media_player.kitchen")],
        [_state("media_player.kitchen", MEDIA_ANNOUNCE)],
    )
    result = await provider.get_media_player_device_infos([MAC.upper()], platform="esphome")
    assert result == {MAC: {"name": "Kitchen", "announce_entity_id": "media_player.kitchen"}}


async def test_entity_without_announce_support() -> None:
    """A matched device without an announce-capable entity still yields its name."""
    provider = _provider(
        [_device()],
        [_entity("media_player.kitchen")],
        [_state("media_player.kitchen", 0)],
    )
    result = await provider.get_media_player_device_infos([MAC], platform="esphome")
    assert result == {MAC: {"name": "Kitchen Speaker", "announce_entity_id": None}}


async def test_ignores_disabled_and_foreign_entities() -> None:
    """Disabled entities and entities of other integrations are not considered."""
    provider = _provider(
        [_device()],
        [
            _entity("media_player.disabled", disabled_by="user"),
            _entity("media_player.other", platform="cast"),
            _entity("sensor.kitchen_temperature"),
        ],
        [],
    )
    result = await provider.get_media_player_device_infos([MAC], platform="esphome")
    assert result == {MAC: {"name": "Kitchen Speaker", "announce_entity_id": None}}


async def test_unknown_devices_are_absent() -> None:
    """Devices unknown to Home Assistant are absent from the result."""
    provider = _provider([_device()], [], [])
    result = await provider.get_media_player_device_infos(["11:22:33:44:55:66"], platform="esphome")
    assert result == {}


async def test_empty_input_skips_registry_fetch() -> None:
    """An empty lookup does not hit the Home Assistant registries."""
    provider = _provider([], [], [])
    assert await provider.get_media_player_device_infos([], platform="esphome") == {}
    provider.hass.get_device_registry.assert_not_awaited()


async def test_play_announcement_on_entity() -> None:
    """An announcement is played via HA's announce feature and awaited for its duration."""
    provider = HomeAssistantProvider.__new__(HomeAssistantProvider)
    provider.hass = SimpleNamespace(call_service=AsyncMock())
    with patch(
        "music_assistant.providers.hass.async_parse_tags",
        AsyncMock(return_value=SimpleNamespace(duration=0.01)),
    ) as parse_tags:
        await provider.play_announcement_on_entity(
            "media_player.kitchen", "http://mass.local/announcement.mp3"
        )
    provider.hass.call_service.assert_awaited_once_with(
        domain="media_player",
        service="play_media",
        service_data={
            "media_content_id": "http://mass.local/announcement.mp3",
            "media_content_type": "music",
            "announce": True,
        },
        target={"entity_id": "media_player.kitchen"},
    )
    parse_tags.assert_awaited_once_with("http://mass.local/announcement.mp3", require_duration=True)
