"""Helper methods for common tasks."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, overload

from soco import SoCo
from soco.exceptions import SoCoException, SoCoUPnPException

from music_assistant.common.models.errors import PlayerCommandFailed

if TYPE_CHECKING:
    from . import SonosPlayer


UID_PREFIX = "RINCON_"
UID_POSTFIX = "01400"

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T", bound="SonosPlayer")
_R = TypeVar("_R")
_P = ParamSpec("_P")

_FuncType = Callable[Concatenate[_T, _P], _R]
_ReturnFuncType = Callable[Concatenate[_T, _P], _R | None]


class SonosUpdateError(PlayerCommandFailed):
    """Update failed."""


@overload
def soco_error(
    errorcodes: None = ...,
) -> Callable[[_FuncType[_T, _P, _R]], _FuncType[_T, _P, _R]]: ...


@overload
def soco_error(
    errorcodes: list[str],
) -> Callable[[_FuncType[_T, _P, _R]], _ReturnFuncType[_T, _P, _R]]: ...


def soco_error(
    errorcodes: list[str] | None = None,
) -> Callable[[_FuncType[_T, _P, _R]], _ReturnFuncType[_T, _P, _R]]:
    """Filter out specified UPnP errors and raise exceptions for service calls."""

    def decorator(funct: _FuncType[_T, _P, _R]) -> _ReturnFuncType[_T, _P, _R]:
        """Decorate functions."""

        def wrapper(self: _T, *args: _P.args, **kwargs: _P.kwargs) -> _R | None:
            """Wrap for all soco UPnP exception."""
            args_soco = next((arg for arg in args if isinstance(arg, SoCo)), None)
            try:
                result = funct(self, *args, **kwargs)
            except (OSError, SoCoException, SoCoUPnPException, TimeoutError) as err:
                error_code = getattr(err, "error_code", None)
                function = funct.__qualname__
                if errorcodes and error_code in errorcodes:
                    _LOGGER.debug("Error code %s ignored in call to %s", error_code, function)
                    return None

                if (target := _find_target_identifier(self, args_soco)) is None:
                    msg = "Unexpected use of soco_error"
                    raise RuntimeError(msg) from err

                message = f"Error calling {function} on {target}: {err}"
                raise SonosUpdateError(message) from err

            return result

        return wrapper

    return decorator


def _find_target_identifier(instance: Any, fallback_soco: SoCo | None) -> str | None:
    """Extract the best available target identifier from the provided instance object."""
    if zone_name := getattr(instance, "zone_name", None):
        # SonosPlayer instance
        return zone_name
    if soco := getattr(instance, "soco", fallback_soco):
        # Holds a SoCo instance attribute
        # Only use attributes with no I/O
        return soco._player_name or soco.ip_address  # pylint: disable=protected-access
    return None


def hostname_to_uid(hostname: str) -> str:
    """Convert a Sonos hostname to a uid."""
    if hostname.startswith("Sonos-"):
        baseuid = hostname.removeprefix("Sonos-").replace(".local.", "")
    elif hostname.startswith("sonos"):
        baseuid = hostname.removeprefix("sonos").replace(".local.", "")
    else:
        msg = f"{hostname} is not a sonos device."
        raise ValueError(msg)
    return f"{UID_PREFIX}{baseuid}{UID_POSTFIX}"


def sync_get_visible_zones(soco: SoCo) -> set[SoCo]:
    """Ensure I/O attributes are cached and return visible zones."""
    _ = soco.household_id
    _ = soco.uid
    return soco.visible_zones
