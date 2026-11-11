"""Shared constants for the config controller package."""

from __future__ import annotations

from typing import Final, TypeVar

from music_assistant_models.config_entries import ConfigValueType

from music_assistant.constants import (
    CONF_ANNOUNCE_VOLUME,
    CONF_ANNOUNCE_VOLUME_MAX,
    CONF_ANNOUNCE_VOLUME_MIN,
    CONF_ANNOUNCE_VOLUME_STRATEGY,
    CONF_AUTO_PLAY,
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_DEVICE_INFO,
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_HIDE_IN_UI,
    CONF_ICON,
    CONF_IDLE_POWER_OFF,
    CONF_LAST_SEEN,
    CONF_LEGACY_IDS,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_MUTE_CONTROL,
    CONF_PLAY_MEDIA_OVERRIDES_GROUP,
    CONF_POWER_CONTROL,
    CONF_PRE_ANNOUNCE_CHIME_URL,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_SESSION_ONLY,
    CONF_TTS_PRE_ANNOUNCE,
    CONF_VOLUME_CONTROL,
)

__all__ = [
    "BASE_KEYS",
    "DEFAULT_SAVE_DELAY",
    "PLAYER_QUEUE_CONFIG_OWNER",
    "PLAYER_VALUE_KEYS",
    "_ConfigValueT",
]

DEFAULT_SAVE_DELAY = 5

BASE_KEYS = (
    "enabled",
    "name",
    "available",
    "default_name",
    "provider",
    "type",
    "player_type",
    # of a protocol: the player it belongs to and the role it has for it
    "player_id",
    "role",
)

# the values of a player's own config: the entries every player gets and what the players
# controller keeps about it. Any other value of a player belongs to one of its protocols.
PLAYER_VALUE_KEYS: Final = frozenset(
    {
        CONF_ANNOUNCE_VOLUME,
        CONF_ANNOUNCE_VOLUME_MAX,
        CONF_ANNOUNCE_VOLUME_MIN,
        CONF_ANNOUNCE_VOLUME_STRATEGY,
        CONF_AUTO_PLAY,
        CONF_CREATED_AT,
        CONF_DEVICE_IDENTIFIERS,
        CONF_DEVICE_INFO,
        CONF_EXPOSE_PLAYER_TO_HA,
        CONF_HIDE_IN_UI,
        CONF_ICON,
        CONF_IDLE_POWER_OFF,
        CONF_LAST_SEEN,
        CONF_LEGACY_IDS,
        CONF_MAX_VOLUME,
        CONF_MIN_VOLUME,
        CONF_MUTE_CONTROL,
        CONF_PLAY_MEDIA_OVERRIDES_GROUP,
        CONF_POWER_CONTROL,
        CONF_PRE_ANNOUNCE_CHIME_URL,
        CONF_PREFERRED_OUTPUT_PROTOCOL,
        CONF_SESSION_ONLY,
        CONF_TTS_PRE_ANNOUNCE,
        CONF_VOLUME_CONTROL,
    }
)

# owner namespace for per-queue config entry strings (controllers/player_queues/strings.json)
PLAYER_QUEUE_CONFIG_OWNER = "core.player_queues"

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)
