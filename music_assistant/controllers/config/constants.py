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
    CONF_CACHED_ARP_MAC,
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_DEVICE_INFO,
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_GROUP_MEMBERS,
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
    CONF_PROTOCOL_DEFAULTS,
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_IDENTIFIERS,
    CONF_REPORTED_MAC,
    CONF_SESSION_ONLY,
    CONF_TTS_PRE_ANNOUNCE,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_VOLUME_CONTROL,
)

__all__ = [
    "AUTOMATIC_VALUE_KEYS",
    "BASE_KEYS",
    "DEFAULT_SAVE_DELAY",
    "GROUP_MEMBER_KEYS",
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
        CONF_PROTOCOL_DEFAULTS,
        CONF_SESSION_ONLY,
        CONF_TTS_PRE_ANNOUNCE,
        CONF_VOLUME_CONTROL,
    }
)

# the values the server or a provider stores by itself, in the record of a player or of a
# protocol, which do not make it one the user customised
AUTOMATIC_VALUE_KEYS: Final = frozenset(
    {
        CONF_CACHED_ARP_MAC,
        CONF_REPORTED_MAC,
        CONF_PROTOCOL_IDENTIFIERS,
        CONF_CREATED_AT,
        CONF_DEVICE_IDENTIFIERS,
        CONF_DEVICE_INFO,
        CONF_LAST_SEEN,
        CONF_LEGACY_IDS,
        CONF_PROTOCOL_DEFAULTS,
        CONF_SESSION_ONLY,
        "sendspin_static_delay",
        "sendspin_opt_out_pending",
        "sendspin_unsupported",
        "stored_volume",
        CONF_PROTOCOL_EXPERIMENTAL_NOTE,
        CONF_UNDERLYING_PLAYER_ID,
    }
)

# the lists of player ids a group stores: its members and the players it allows as one
GROUP_MEMBER_KEYS: Final = (CONF_GROUP_MEMBERS, "allowed_members")

# owner namespace for per-queue config entry strings (controllers/player_queues/strings.json)
PLAYER_QUEUE_CONFIG_OWNER = "core.player_queues"

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)
