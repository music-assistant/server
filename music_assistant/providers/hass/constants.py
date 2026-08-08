"""Constants for the Home Assistant provider."""

from __future__ import annotations

from enum import IntFlag
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import PlaybackState

if TYPE_CHECKING:
    import logging

CONF_POWER_CONTROLS = "power_controls"
CONF_MUTE_CONTROLS = "mute_controls"
CONF_VOLUME_CONTROLS = "volume_controls"

# Home Assistant entity domains Music Assistant can offer as player controls.
CONTROL_DOMAINS = ("media_player", "switch", "input_boolean", "number", "input_number")


class MediaPlayerEntityFeature(IntFlag):
    """Supported features of the media player entity."""

    PAUSE = 1
    SEEK = 2
    VOLUME_SET = 4
    VOLUME_MUTE = 8
    PREVIOUS_TRACK = 16
    NEXT_TRACK = 32

    TURN_ON = 128
    TURN_OFF = 256
    PLAY_MEDIA = 512
    VOLUME_STEP = 1024
    SELECT_SOURCE = 2048
    STOP = 4096
    CLEAR_PLAYLIST = 8192
    PLAY = 16384
    SHUFFLE_SET = 32768
    SELECT_SOUND_MODE = 65536
    BROWSE_MEDIA = 131072
    REPEAT_SET = 262144
    GROUPING = 524288
    MEDIA_ANNOUNCE = 1048576
    MEDIA_ENQUEUE = 2097152


StateMap = {
    "playing": PlaybackState.PLAYING,
    "paused": PlaybackState.PAUSED,
    "buffering": PlaybackState.PLAYING,
    "idle": PlaybackState.IDLE,
    "off": PlaybackState.IDLE,
    "standby": PlaybackState.IDLE,
    "unknown": PlaybackState.IDLE,
    "unavailable": PlaybackState.IDLE,
}

# HA states that we consider as "powered off"
OFF_STATES = ("unavailable", "unknown", "standby", "off")
UNAVAILABLE_STATES = ("unavailable", "unknown")


def parse_supported_features(
    raw_value: Any, entity_id: str, logger: logging.Logger
) -> MediaPlayerEntityFeature:
    """
    Return the features supported by a Home Assistant media_player entity.

    A value that can not be interpreted yields no features at all.

    :param raw_value: Raw value of the entity's supported_features attribute.
    :param entity_id: Entity id the value belongs to, used for logging.
    :param logger: Logger to report an invalid value on.
    """
    if raw_value is None:
        # attribute is absent for entities that (currently) have no state
        return MediaPlayerEntityFeature(0)
    if isinstance(raw_value, int) and not isinstance(raw_value, bool) and raw_value >= 0:
        # unknown bits (features of a newer HA version) are preserved
        return MediaPlayerEntityFeature(raw_value)
    # integrations are free to write anything into the attribute, so a value we can not
    # interpret must not break the handling of all other entities
    logger.warning(
        "Home Assistant entity %s reports an invalid supported_features value: %r - "
        "treating it as if it supports no features",
        entity_id,
        raw_value,
    )
    return MediaPlayerEntityFeature(0)
