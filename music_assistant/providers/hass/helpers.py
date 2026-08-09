"""Helpers for interpreting Home Assistant entities as player controls."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from .constants import MediaPlayerEntityFeature, parse_supported_features

if TYPE_CHECKING:
    import logging

    from hass_client.models import State

# Home Assistant entity IDs are a domain and an object ID, both lowercase, joined by a dot
ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class ControlCapabilities(NamedTuple):
    """The player control roles a Home Assistant entity can serve."""

    power: bool = False
    volume: bool = False
    mute: bool = False


def is_entity_id(value: str) -> bool:
    """
    Return whether the given value has the shape of a Home Assistant entity ID.

    :param value: The value to inspect.
    """
    return bool(ENTITY_ID_PATTERN.match(value))


def get_control_capabilities(state: State, logger: logging.Logger) -> ControlCapabilities:
    """
    Return the player control roles the given Home Assistant entity can serve.

    :param state: The current state of the entity to inspect.
    :param logger: Logger to report an unparsable supported_features attribute on.
    :return: The supported roles; all False when the entity is unusable as a player control.
    """
    entity_platform = state["entity_id"].split(".")[0]
    if entity_platform in ("switch", "input_boolean"):
        # simple on/off controls are suitable as power and mute controls
        return ControlCapabilities(power=True, mute=True)
    if entity_platform in ("number", "input_number"):
        # number and input_number are very similar, both are suitable for volume control
        return ControlCapabilities(volume=True)
    # media player can be used as control, depending on features
    if entity_platform != "media_player":
        return ControlCapabilities()
    if "mass_player_type" in state["attributes"]:
        # filter out mass players
        return ControlCapabilities()
    supported_features = parse_supported_features(
        state["attributes"].get("supported_features"),
        state["entity_id"],
        logger,
    )
    return ControlCapabilities(
        power=(
            MediaPlayerEntityFeature.TURN_ON in supported_features
            and MediaPlayerEntityFeature.TURN_OFF in supported_features
        ),
        volume=MediaPlayerEntityFeature.VOLUME_SET in supported_features,
        mute=MediaPlayerEntityFeature.VOLUME_MUTE in supported_features,
    )


def get_control_name(entity_id: str, state: State | None) -> str:
    """
    Return the human readable name to present a Home Assistant entity control under.

    :param entity_id: The entity the control is based on.
    :param state: The entity's current state, if known.
    """
    if state and (friendly_name := state["attributes"].get("friendly_name")):
        return f"{friendly_name} ({entity_id})"
    return entity_id
