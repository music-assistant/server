"""MA Player ↔ Yandex Smart Home device mapper.

Maps Music Assistant Player state to Yandex Smart Home device descriptions,
capability states, and action execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import PlaybackState

from .constants import (
    ERROR_DEVICE_UNREACHABLE,
    ERROR_INTERNAL_ERROR,
    ERROR_INVALID_ACTION,
    INSTANCE_CHANNEL,
    INSTANCE_INPUT_SOURCE,
    INSTANCE_MUTE,
    INSTANCE_ON,
    INSTANCE_PAUSE,
    INSTANCE_VOLUME,
    UNIT_PERCENT,
    YANDEX_DEVICE_TYPE_RECEIVER,
    YANDEX_MODE_VALUES,
)
from .schema import (
    ActionResult,
    CapabilityAction,
    CapabilityActionResult,
    CapabilityActionResultState,
    CapabilityDescription,
    CapabilityInstanceState,
    CapabilityParameters,
    CapabilityState,
    DeviceDescription,
    DeviceState,
    ModeValue,
    RangeParameters,
    YandexCapabilityType,
    YandexDeviceInfo,
)

if TYPE_CHECKING:
    from music_assistant_models.player import Player, PlayerSource

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source list helpers (for input_source capability)
# ---------------------------------------------------------------------------


def _supports_select_source(player: Player) -> bool:
    """Check if player natively supports source selection."""
    features = getattr(player, "supported_features", None)
    if not features:
        return False
    # PlayerFeature.SELECT_SOURCE == "select_source"
    return any(
        str(f) == "select_source" or getattr(f, "value", None) == "select_source" for f in features
    )


def _get_source_list(player: Player) -> list[PlayerSource]:
    """Get the source list from a player, or empty list if not available."""
    if not _supports_select_source(player):
        return []
    source_list = getattr(player, "source_list", None)
    if source_list:
        return list(source_list)
    return []


def _build_source_modes(source_list: list[PlayerSource]) -> list[ModeValue]:
    """Build Yandex mode values from an MA source list (max 10)."""
    return [ModeValue(value=YANDEX_MODE_VALUES[i]) for i in range(min(len(source_list), 10))]


def _source_to_mode(active_source: str | None, source_list: list[PlayerSource]) -> str | None:
    """Map active MA source name/id to a Yandex mode value."""
    if not active_source or not source_list:
        return None
    for i, source in enumerate(source_list[:10]):
        source_name = getattr(source, "name", str(source))
        source_id = getattr(source, "id", str(source))
        if active_source in (source_name, source_id):
            return YANDEX_MODE_VALUES[i]
    return None


def _mode_to_source(mode_value: str, source_list: list[PlayerSource]) -> str | None:
    """Resolve a Yandex mode value to an MA source name."""
    try:
        idx = list(YANDEX_MODE_VALUES).index(mode_value)
    except ValueError:
        return None
    if idx >= len(source_list):
        return None
    return source_list[idx].name


# ---------------------------------------------------------------------------
# Device description & state
# ---------------------------------------------------------------------------


def _volume_range_params() -> CapabilityParameters:
    """Build range parameters for volume capability."""
    return CapabilityParameters(
        instance=INSTANCE_VOLUME,
        range=RangeParameters(min=0, max=100, precision=1),
        unit=UNIT_PERCENT,
    )


def get_device_description(player: Player) -> DeviceDescription:
    """Build a Yandex Smart Home device description from an MA player."""
    capabilities = [
        CapabilityDescription(type=YandexCapabilityType.ON_OFF),
        CapabilityDescription(
            type=YandexCapabilityType.RANGE,
            parameters=_volume_range_params(),
        ),
        CapabilityDescription(
            type=YandexCapabilityType.TOGGLE,
            parameters=CapabilityParameters(instance=INSTANCE_MUTE),
        ),
        CapabilityDescription(
            type=YandexCapabilityType.TOGGLE,
            parameters=CapabilityParameters(instance=INSTANCE_PAUSE),
        ),
        CapabilityDescription(
            type=YandexCapabilityType.RANGE,
            parameters=CapabilityParameters(
                instance=INSTANCE_CHANNEL,
                range=RangeParameters(min=0, max=999, precision=1),
                random_access=False,
            ),
        ),
    ]

    # mode(input_source) — only if player has sources
    source_list = _get_source_list(player)
    if source_list:
        modes = _build_source_modes(source_list)
        if modes:
            capabilities.append(
                CapabilityDescription(
                    type=YandexCapabilityType.MODE,
                    parameters=CapabilityParameters(
                        instance=INSTANCE_INPUT_SOURCE,
                        modes=modes,
                    ),
                )
            )

    model = "MA Player"
    if hasattr(player, "device_info") and player.device_info:
        model = getattr(player.device_info, "model", model) or model

    return DeviceDescription(
        id=player.player_id,
        name=player.name,
        type=YANDEX_DEVICE_TYPE_RECEIVER,
        capabilities=capabilities,
        device_info=YandexDeviceInfo(model=model),
    )


def get_device_state(player: Player) -> DeviceState:
    """Read current MA player state and convert to Yandex capability states."""
    is_on = player.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
    is_paused = player.playback_state == PlaybackState.PAUSED
    volume = player.volume_level if player.volume_level is not None else 0
    muted = player.volume_muted if player.volume_muted is not None else False

    capabilities = [
        CapabilityState(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityInstanceState(instance=INSTANCE_ON, value=is_on),
        ),
        CapabilityState(
            type=YandexCapabilityType.RANGE,
            state=CapabilityInstanceState(instance=INSTANCE_VOLUME, value=volume),
        ),
        CapabilityState(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityInstanceState(instance=INSTANCE_MUTE, value=muted),
        ),
        CapabilityState(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityInstanceState(instance=INSTANCE_PAUSE, value=is_paused),
        ),
        CapabilityState(
            type=YandexCapabilityType.RANGE,
            state=CapabilityInstanceState(instance=INSTANCE_CHANNEL, value=0),
        ),
    ]

    # input_source state — only if player has sources
    source_list = _get_source_list(player)
    if source_list:
        active = getattr(player, "active_source", None)
        mode_value = _source_to_mode(active, source_list)
        if mode_value:
            capabilities.append(
                CapabilityState(
                    type=YandexCapabilityType.MODE,
                    state=CapabilityInstanceState(instance=INSTANCE_INPUT_SOURCE, value=mode_value),
                )
            )

    return DeviceState(id=player.player_id, capabilities=capabilities)


async def execute_capability_action(
    mass: Any,
    player_id: str,
    action: CapabilityAction,
    current_volume: int = 0,
) -> CapabilityActionResult:
    """Execute a Yandex capability action by calling the corresponding MA player command.

    Returns a CapabilityActionResult with success or error status.
    """
    instance = action.state.instance
    value = action.state.value

    try:
        if action.type == YandexCapabilityType.ON_OFF:
            if value:
                await mass.players.cmd_play(player_id)
            else:
                await mass.players.cmd_stop(player_id)

        elif action.type == YandexCapabilityType.RANGE and instance == INSTANCE_VOLUME:
            if action.state.relative:
                target = max(0, min(100, current_volume + int(value)))
            else:
                target = max(0, min(100, int(value)))
            await mass.players.cmd_volume_set(player_id, target)
            value = target

        elif action.type == YandexCapabilityType.TOGGLE and instance == INSTANCE_MUTE:
            await mass.players.cmd_volume_mute(player_id, bool(value))

        elif action.type == YandexCapabilityType.TOGGLE and instance == INSTANCE_PAUSE:
            if value:
                await mass.players.cmd_pause(player_id)
            else:
                await mass.players.cmd_play(player_id)

        elif action.type == YandexCapabilityType.RANGE and instance == INSTANCE_CHANNEL:
            if action.state.relative:
                if int(value) > 0:
                    await mass.players.cmd_next_track(player_id)
                elif int(value) < 0:
                    await mass.players.cmd_previous_track(player_id)
            # Non-relative channel set is ignored (no concept of channel number in MA)

        elif action.type == YandexCapabilityType.MODE and instance == INSTANCE_INPUT_SOURCE:
            player = mass.players.get_player(player_id)
            p_state = player.state if hasattr(player, "state") else player
            source_list = _get_source_list(p_state)
            source = _mode_to_source(str(value), source_list)
            if source:
                await mass.players.select_source(player_id, source)
            else:
                return CapabilityActionResult(
                    type=action.type,
                    state=CapabilityActionResultState(
                        instance=instance,
                        action_result=ActionResult(
                            status="ERROR",
                            error_code=ERROR_INVALID_ACTION,
                            error_message=f"Unknown source mode: {value}",
                        ),
                    ),
                )

        else:
            return CapabilityActionResult(
                type=action.type,
                state=CapabilityActionResultState(
                    instance=instance,
                    action_result=ActionResult(
                        status="ERROR",
                        error_code=ERROR_INVALID_ACTION,
                        error_message=f"Unknown capability: {action.type}/{instance}",
                    ),
                ),
            )

    except Exception:
        _LOGGER.exception("Error executing action %s/%s on %s", action.type, instance, player_id)
        return CapabilityActionResult(
            type=action.type,
            state=CapabilityActionResultState(
                instance=instance,
                action_result=ActionResult(
                    status="ERROR",
                    error_code=ERROR_INTERNAL_ERROR,
                ),
            ),
        )

    return CapabilityActionResult(
        type=action.type,
        state=CapabilityActionResultState(
            instance=instance,
            value=value,
            action_result=ActionResult(status="DONE"),
        ),
    )


def is_player_exposable(player: Player, exposed_ids: set[str] | None = None) -> bool:
    """Determine whether an MA player should be exposed to Yandex Smart Home."""
    if not player.available:
        return False
    if not player.enabled:
        return False
    # Don't expose players that are synced to another player (they are controlled via leader)
    if player.synced_to:
        return False
    # If a filter is set, only expose selected players
    return not (exposed_ids and player.player_id not in exposed_ids)


def make_error_device_state(device_id: str) -> DeviceState:
    """Create an error DeviceState for an unreachable device."""
    return DeviceState(
        id=device_id,
        error_code=ERROR_DEVICE_UNREACHABLE,
        error_message="Device is not available",
    )


def make_error_action_result(
    _device_id: str, actions: list[CapabilityAction]
) -> list[CapabilityActionResult]:
    """Create error action results for all capabilities of an unreachable device."""
    return [
        CapabilityActionResult(
            type=a.type,
            state=CapabilityActionResultState(
                instance=a.state.instance,
                action_result=ActionResult(
                    status="ERROR",
                    error_code=ERROR_DEVICE_UNREACHABLE,
                ),
            ),
        )
        for a in actions
    ]
