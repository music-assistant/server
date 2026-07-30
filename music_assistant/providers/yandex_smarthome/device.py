"""
MA Player ↔ Yandex Smart Home device mapper.

Maps Music Assistant Player state to Yandex Smart Home device descriptions,
capability states, and action execution.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
    MAX_INPUT_SOURCES,
    UNIT_PERCENT,
    YANDEX_DEVICE_TYPE_MEDIA,
    YANDEX_MODE_VALUES,
)
from .playlists import play_playlist
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


def _is_group_player(player: Player) -> bool:
    """Check if player is a group (has child members)."""
    group_members = getattr(player, "group_members", None)
    return bool(group_members)


def _has_feature(player: Player, feature_name: str) -> bool:
    """Check if player supports a given PlayerFeature by name."""
    features = getattr(player, "supported_features", None)
    if not features:
        return False
    return any(
        str(f) == feature_name or getattr(f, "value", None) == feature_name for f in features
    )


def _supports_select_source(player: Player) -> bool:
    """Check if player natively supports source selection."""
    return _has_feature(player, "select_source")


def _get_source_list(player: Player) -> list[PlayerSource]:
    """Get the source list from a player, or empty list if not available."""
    if not _supports_select_source(player):
        return []
    source_list = getattr(player, "source_list", None)
    if source_list:
        return list(source_list)
    return []


def _combined_size(
    source_list: list[PlayerSource], playlist_uris: tuple[str, ...] | list[str]
) -> int:
    """Total slot count for mode(input_source): native first, playlists fill rest."""
    return min(len(source_list) + len(playlist_uris), MAX_INPUT_SOURCES)


def _build_combined_modes(
    source_list: list[PlayerSource], playlist_uris: tuple[str, ...] | list[str]
) -> list[ModeValue]:
    """Build mode values covering both native sources and playlist slots."""
    size = _combined_size(source_list, playlist_uris)
    return [ModeValue(value=YANDEX_MODE_VALUES[i]) for i in range(size)]


def _resolve_combined_slot(
    index: int,
    source_list: list[PlayerSource],
    playlist_uris: tuple[str, ...] | list[str],
) -> tuple[str, str] | None:
    """
    Resolve a 0-based slot index to ("native"|"playlist", value).

    Returns None if the slot is out of range. The native source slots come
    first; playlist URIs fill the remainder up to MAX_INPUT_SOURCES.
    """
    native_count = len(source_list)
    if index < 0:
        return None
    if index < native_count:
        return ("native", source_list[index].id)
    playlist_idx = index - native_count
    if playlist_idx >= len(playlist_uris):
        return None
    if native_count + playlist_idx >= MAX_INPUT_SOURCES:
        return None
    return ("playlist", playlist_uris[playlist_idx])


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
    """Resolve a Yandex mode value to an MA source id."""
    try:
        idx = YANDEX_MODE_VALUES.index(mode_value)
    except ValueError:
        return None
    if idx >= len(source_list):
        return None
    return source_list[idx].id


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


# Yandex Smart Home allows only Russian/English letters, digits, and spaces.
_RE_DISALLOWED = re.compile(r"[^a-zA-Zа-яА-ЯёЁ0-9 ]")  # noqa: RUF001
_RE_LETTER_DIGIT = re.compile(r"([a-zA-Zа-яА-ЯёЁ])(\d)")  # noqa: RUF001
_RE_DIGIT_LETTER = re.compile(r"(\d)([a-zA-Zа-яА-ЯёЁ])")  # noqa: RUF001
_RE_MULTI_SPACE = re.compile(r" {2,}")


def normalize_device_name(name: str) -> str:
    """
    Normalize player name for Yandex Smart Home.

    Rules: only Russian/English letters, digits, and spaces;
    mandatory space between letters and digits.
    """
    result = _RE_DISALLOWED.sub(" ", name)
    result = _RE_LETTER_DIGIT.sub(r"\1 \2", result)
    result = _RE_DIGIT_LETTER.sub(r"\1 \2", result)
    result = _RE_MULTI_SPACE.sub(" ", result).strip()
    return result or name


def get_device_description(
    player: Player,
    *,
    playlist_uris: tuple[str, ...] | list[str] = (),
) -> DeviceDescription:
    """Build a Yandex Smart Home device description from an MA player."""
    capabilities = [
        CapabilityDescription(type=YandexCapabilityType.ON_OFF),
        CapabilityDescription(
            type=YandexCapabilityType.RANGE,
            parameters=_volume_range_params(),
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

    # toggle(mute) — if player supports VOLUME_MUTE or is a group
    if _has_feature(player, "volume_mute") or _is_group_player(player):
        capabilities.append(
            CapabilityDescription(
                type=YandexCapabilityType.TOGGLE,
                parameters=CapabilityParameters(instance=INSTANCE_MUTE),
            )
        )

    # mode(input_source): register when native sources or playlists exist.
    # Native sources occupy first slots; playlists fill remainder up to MAX_INPUT_SOURCES.
    source_list = _get_source_list(player)
    if len(source_list) >= MAX_INPUT_SOURCES and playlist_uris:
        # Debug-level: this fires on every /user/devices poll for an
        # affected player, but it documents a config decision rather than
        # a runtime fault — promoting it to warn would spam production logs.
        _LOGGER.debug(
            "Player %s has %d native sources (>= cap %d); playlist sources ignored",
            player.player_id,
            len(source_list),
            MAX_INPUT_SOURCES,
        )
    modes = _build_combined_modes(source_list, playlist_uris)
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
        name=normalize_device_name(player.name),
        type=YANDEX_DEVICE_TYPE_MEDIA,
        capabilities=capabilities,
        device_info=YandexDeviceInfo(model=model),
    )


def get_device_state(
    player: Player,
    *,
    playlist_uris: tuple[str, ...] | list[str] = (),
) -> DeviceState:
    """Read current MA player state and convert to Yandex capability states."""
    # on = player is powered on (or available if power state unknown)
    powered = getattr(player, "powered", None)
    is_on = powered if powered is not None else getattr(player, "available", True)
    is_paused = player.playback_state != PlaybackState.PLAYING
    is_group = _is_group_player(player)

    # For groups use group_volume/group_volume_muted which aggregate children
    if is_group:
        volume = getattr(player, "group_volume", None)
        if volume is None:
            volume = player.volume_level if player.volume_level is not None else 0
    else:
        volume = player.volume_level if player.volume_level is not None else 0

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
            state=CapabilityInstanceState(instance=INSTANCE_PAUSE, value=is_paused),
        ),
        CapabilityState(
            type=YandexCapabilityType.RANGE,
            state=CapabilityInstanceState(instance=INSTANCE_CHANNEL, value=0),
        ),
    ]

    # mute state — if player supports VOLUME_MUTE or is a group
    if _has_feature(player, "volume_mute") or is_group:
        if is_group:
            muted = getattr(player, "group_volume_muted", None)
            if muted is None:
                muted = player.volume_muted if player.volume_muted is not None else False
        else:
            muted = player.volume_muted if player.volume_muted is not None else False
        capabilities.append(
            CapabilityState(
                type=YandexCapabilityType.TOGGLE,
                state=CapabilityInstanceState(instance=INSTANCE_MUTE, value=muted),
            )
        )

    # input_source state — only reported when active MA source matches a native slot.
    # Playlist slots have no reliable "active" signal, so they leave state unset
    # (Yandex tolerates an unreported state on mode capabilities).
    source_list = _get_source_list(player)
    if source_list or playlist_uris:
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


async def _execute_input_source(
    mass: Any,
    player_id: str,
    player: Player | None,
    instance: str,
    value: Any,
    *,
    playlist_uris: tuple[str, ...] | list[str] = (),
) -> CapabilityActionResult | None:
    """Handle input_source mode action. Returns error result or None on success."""
    if player is None:
        return CapabilityActionResult(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionResultState(
                instance=instance,
                action_result=ActionResult(
                    status="ERROR",
                    error_code=ERROR_DEVICE_UNREACHABLE,
                    error_message=f"Player {player_id} not found",
                ),
            ),
        )

    try:
        slot_index = YANDEX_MODE_VALUES.index(str(value))
    except ValueError:
        return CapabilityActionResult(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionResultState(
                instance=instance,
                action_result=ActionResult(
                    status="ERROR",
                    error_code=ERROR_INVALID_ACTION,
                    error_message=f"Unknown source mode: {value}",
                ),
            ),
        )

    p_state = player.state if hasattr(player, "state") else player
    source_list = _get_source_list(p_state)
    resolved = _resolve_combined_slot(slot_index, source_list, playlist_uris)
    if resolved is None:
        return CapabilityActionResult(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionResultState(
                instance=instance,
                action_result=ActionResult(
                    status="ERROR",
                    error_code=ERROR_INVALID_ACTION,
                    error_message=f"No source configured for mode: {value}",
                ),
            ),
        )

    kind, target = resolved
    if kind == "native":
        await mass.players.select_source(player_id, target)
        return None

    # Playlist slot: power on if needed, then start playback via player_queues.play_media.
    if _has_feature(p_state, "power"):
        powered = getattr(p_state, "powered", None)
        if powered is False:
            await mass.players.cmd_power(player_id, True)
    await play_playlist(mass, player_id, target)
    return None


def _invalid_bool_result(cap_type: str, instance: str, value: Any) -> CapabilityActionResult:
    """Build an INVALID_ACTION result for a capability that requires a boolean value."""
    return CapabilityActionResult(
        type=cap_type,
        state=CapabilityActionResultState(
            instance=instance,
            action_result=ActionResult(
                status="ERROR",
                error_code=ERROR_INVALID_ACTION,
                error_message=(
                    f"Expected boolean value for {cap_type}/{instance}, got {type(value).__name__}"
                ),
            ),
        ),
    )


def _invalid_numeric_result(cap_type: str, instance: str, value: Any) -> CapabilityActionResult:
    """Build an INVALID_ACTION result for a capability that requires a numeric value."""
    return CapabilityActionResult(
        type=cap_type,
        state=CapabilityActionResultState(
            instance=instance,
            action_result=ActionResult(
                status="ERROR",
                error_code=ERROR_INVALID_ACTION,
                error_message=(
                    f"Expected numeric value for {cap_type}/{instance}, got {type(value).__name__}"
                ),
            ),
        ),
    )


async def execute_capability_action(  # noqa: PLR0915
    mass: Any,
    player_id: str,
    action: CapabilityAction,
    current_volume: int = 0,
    *,
    playlist_uris: tuple[str, ...] | list[str] = (),
) -> CapabilityActionResult:
    """
    Execute a Yandex capability action by calling the corresponding MA player command.

    Returns a CapabilityActionResult with success or error status.
    """
    instance = action.state.instance
    value = action.state.value
    player = mass.players.get_player(player_id)

    if player is None:
        return CapabilityActionResult(
            type=action.type,
            state=CapabilityActionResultState(
                instance=instance,
                action_result=ActionResult(
                    status="ERROR",
                    error_code=ERROR_DEVICE_UNREACHABLE,
                    error_message=f"Player {player_id} not found",
                ),
            ),
        )

    is_group = _is_group_player(player)

    # Bool-typed capabilities: reject non-bool payloads up-front so truthiness
    # on strings like "false"/"0" can't trigger the wrong command.
    requires_bool = action.type == YandexCapabilityType.ON_OFF or (
        action.type == YandexCapabilityType.TOGGLE and instance in (INSTANCE_MUTE, INSTANCE_PAUSE)
    )
    if requires_bool and not isinstance(value, bool):
        return _invalid_bool_result(action.type, instance, value)

    # Numeric RANGE capabilities: reject bool explicitly (bool is a subclass of
    # int in Python, so float(True)/int(False) would silently change volume or
    # skip tracks) and any other non-numeric type.
    if action.type == YandexCapabilityType.RANGE and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        return _invalid_numeric_result(action.type, instance, value)

    try:
        if action.type == YandexCapabilityType.ON_OFF:
            if value:
                # Power on if supported, then play
                if _has_feature(player, "power"):
                    await mass.players.cmd_power(player_id, True)
                await mass.players.cmd_play(player_id)
            else:
                await mass.players.cmd_stop(player_id)
                if _has_feature(player, "power"):
                    await mass.players.cmd_power(player_id, False)

        elif action.type == YandexCapabilityType.RANGE and instance == INSTANCE_VOLUME:
            if action.state.relative:
                target = max(0, min(100, current_volume + int(float(value))))
            else:
                target = max(0, min(100, int(float(value))))
            if is_group:
                await mass.players.cmd_group_volume(player_id, target)
            else:
                await mass.players.cmd_volume_set(player_id, target)
            value = target

        elif action.type == YandexCapabilityType.TOGGLE and instance == INSTANCE_MUTE:
            if is_group:
                await mass.players.cmd_group_volume_mute(player_id, value)
            else:
                await mass.players.cmd_volume_mute(player_id, value)

        elif action.type == YandexCapabilityType.TOGGLE and instance == INSTANCE_PAUSE:
            if value:
                await mass.players.cmd_pause(player_id)
            else:
                await mass.players.cmd_play(player_id)

        elif action.type == YandexCapabilityType.RANGE and instance == INSTANCE_CHANNEL:
            if action.state.relative:
                if int(float(value)) > 0:
                    await mass.players.cmd_next_track(player_id)
                elif int(float(value)) < 0:
                    await mass.players.cmd_previous_track(player_id)
            # Non-relative channel set is ignored (no concept of channel number in MA)

        elif action.type == YandexCapabilityType.MODE and instance == INSTANCE_INPUT_SOURCE:
            result = await _execute_input_source(
                mass, player_id, player, instance, value, playlist_uris=playlist_uris
            )
            if result:
                return result

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

    except ValueError, TypeError:
        return CapabilityActionResult(
            type=action.type,
            state=CapabilityActionResultState(
                instance=instance,
                action_result=ActionResult(
                    status="ERROR",
                    error_code=ERROR_INVALID_ACTION,
                    error_message=f"Invalid value for {action.type}/{instance}: {value}",
                ),
            ),
        )
    except asyncio.CancelledError:
        # Cooperative cancellation must propagate untouched — without this
        # the broad `except Exception` below would convert a shutdown /
        # config-flow abort into an INTERNAL_ERROR action result.
        raise
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
