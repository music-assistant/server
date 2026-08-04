"""Tests for correlating natively connected devices to their Home Assistant representation."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.player import PlayerMedia

from music_assistant.providers.hass import HomeAssistantProvider

MEDIA_ANNOUNCE = 1048576
MAC = "aa:bb:cc:dd:ee:ff"


class _Cache:
    """Provide the slice of the cache controller that @use_cache relies on."""

    def __init__(self) -> None:
        self.entries: dict[str, Any] = {}

    async def get_with_freshness(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        """Return the (data, is_fresh, found) triplet for the given key."""
        await asyncio.sleep(0)
        if key not in self.entries:
            return None, False, False
        return self.entries[key], True, True

    async def set(self, key: str, data: Any, **kwargs: Any) -> None:
        """Store data under the given key."""
        self.entries[key] = data


def _provider(
    devices: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    states: list[dict[str, Any]],
) -> HomeAssistantProvider:
    async def _send_command(command: str, **_kwargs: Any) -> dict[str, Any]:
        assert command == "config/entity_registry/list_for_display"
        # Home Assistant leaves disabled entities out of the registry listing
        return {
            "entity_categories": {},
            "entities": [
                {"ei": entity["entity_id"], "pl": entity["platform"], "di": entity["device_id"]}
                for entity in entities
                if entity["disabled_by"] is None
            ],
        }

    provider = HomeAssistantProvider.__new__(HomeAssistantProvider)
    provider.hass = SimpleNamespace(
        get_device_registry=AsyncMock(return_value=devices),
        send_command=AsyncMock(side_effect=_send_command),
    )
    provider._entity_registry = None
    provider._entity_registry_lock = asyncio.Lock()
    # the device registry lookup runs through @use_cache, which needs a cache to talk to
    provider.config = SimpleNamespace(instance_id="hass--test")  # type: ignore[assignment]
    provider.manifest = SimpleNamespace(domain="hass")  # type: ignore[assignment]
    provider.mass = SimpleNamespace(  # type: ignore[assignment]
        cache=_Cache(), create_task=asyncio.create_task
    )
    provider.get_states = AsyncMock(return_value=states)  # type: ignore[method-assign]
    provider.logger = logging.getLogger("test.hass")
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
    provider.mass = MagicMock()
    provider.mass.streams.get_announcement_duration = AsyncMock(return_value=7)
    announcement = PlayerMedia(uri="http://mass.local/announcement.mp3")
    with patch("music_assistant.providers.hass.asyncio.sleep", AsyncMock()) as sleep:
        await provider.play_announcement_on_entity("media_player.kitchen", announcement)
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
    # the length is resolved after the announcement was handed to the entity
    provider.mass.streams.get_announcement_duration.assert_awaited_once_with(announcement)
    sleep.assert_awaited_once_with(7)
