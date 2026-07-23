"""Helpers and utilities for the Home Assistant PlayerProvider."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, TypedDict, cast

from music_assistant_models.enums import IdentifierType
from music_assistant_models.errors import InvalidDataError, LoginFailed

from music_assistant.providers.hass.constants import MediaPlayerEntityFeature

from .constants import BLOCKLISTED_HASS_INTEGRATIONS, DOMAIN

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from hass_client.models import Entity as HassEntity
    from hass_client.models import State as HassState

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.hass import HomeAssistantProvider


async def get_hass_media_players(
    hass_prov: HomeAssistantProvider,
) -> AsyncGenerator[tuple[HassState, HassEntity | None]]:
    """Return all HA state objects (with registry entry) for (valid) media_player entities."""
    entity_registry = {x["entity_id"]: x for x in await hass_prov.hass.get_entity_registry()}
    # discover via the registry instead of a full state dump; entities without a
    # unique_id are not registered and are therefore not discovered here
    media_player_ids = [
        entity_id for entity_id in entity_registry if entity_id.startswith("media_player.")
    ]
    for state in await hass_prov.get_states(entity_ids=media_player_ids):
        if "mass_player_type" in state["attributes"]:
            # filter out mass players
            continue
        if "friendly_name" not in state["attributes"]:
            # filter out invalid/unavailable players
            continue
        supported_features = MediaPlayerEntityFeature(state["attributes"]["supported_features"])
        if MediaPlayerEntityFeature.PLAY_MEDIA not in supported_features:
            continue
        entity_registry_entry = entity_registry.get(state["entity_id"])
        if entity_registry_entry is not None:
            hass_domain = entity_registry_entry["platform"]
            if hass_domain in BLOCKLISTED_HASS_INTEGRATIONS:
                continue
        yield state, entity_registry_entry


def normalized_mac(mac: str) -> str:
    """Normalize a MAC address for comparison (lowercase, no separators)."""
    return mac.replace(":", "").replace("-", "").lower()


def native_player_macs(mass: MusicAssistant) -> set[str]:
    """
    Collect the normalized MAC addresses of all natively registered players.

    Used to detect HA entities that point to a device that is already available
    as a native Music Assistant player (e.g. an ESPHome device using Sendspin).
    """
    macs: set[str] = set()
    for player in mass.players:
        if player.provider.domain == DOMAIN:
            continue
        if mac := player.device_info.identifiers.get(IdentifierType.MAC_ADDRESS):
            macs.add(normalized_mac(mac))
    return macs


class ESPHomeSupportedAudioFormat(TypedDict):
    """ESPHome Supported Audio Format."""

    format: str  # flac, wav or mp3
    sample_rate: int  # e.g. 48000
    num_channels: int  # 1 for announcements, 2 for media
    purpose: int  # 0 for media, 1 for announcements
    sample_bytes: int  # 1 for 8 bit, 2 for 16 bit, 4 for 32 bit


async def get_esphome_supported_audio_formats(
    hass_prov: HomeAssistantProvider, conf_entry_id: str
) -> list[ESPHomeSupportedAudioFormat]:
    """Get supported audio formats for an ESPHome device."""
    result: list[ESPHomeSupportedAudioFormat] = []
    try:
        # TODO: expose this in the hass client lib instead of hacking around private vars
        ws_url = hass_prov.hass._websocket_url or "ws://supervisor/core/websocket"
        hass_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
        hass_url = hass_url.replace("/api/websocket", "").replace("/websocket", "")
        api_token = hass_prov.hass._token or os.environ.get("HASSIO_TOKEN")
        url = f"{hass_url}/api/diagnostics/config_entry/{conf_entry_id}"
        headers = {
            "Authorization": f"Bearer {api_token}",
            "content-type": "application/json",
        }
        async with hass_prov.mass.http_session.get(url, headers=headers) as response:
            if response.status != 200:
                raise LoginFailed("Unable to contact Home Assistant to retrieve diagnostics")
            data = await response.json()
            if "data" not in data or "storage_data" not in data["data"]:
                return result
            if "media_player" not in data["data"]["storage_data"]:
                raise InvalidDataError("Media player info not found in ESPHome diagnostics")
            for media_player_obj in data["data"]["storage_data"]["media_player"]:
                if "supported_formats" not in media_player_obj:
                    continue
                for supported_format_obj in media_player_obj["supported_formats"]:
                    result.append(cast("ESPHomeSupportedAudioFormat", supported_format_obj))
    except Exception as exc:
        hass_prov.logger.warning(
            "Failed to fetch diagnostics for ESPHome player: %s",
            str(exc),
            exc_info=exc if hass_prov.logger.isEnabledFor(logging.DEBUG) else None,
        )
    return result
