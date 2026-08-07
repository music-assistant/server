"""Helper methods for common tasks."""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast, overload

from music_assistant_models.errors import PlayerCommandFailed
from soco.exceptions import SoCoException, SoCoUPnPException

from .constants import UID_POSTFIX, UID_PREFIX

if TYPE_CHECKING:
    from soco import SoCo

    from .player import SonosPlayer


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
        if inspect.iscoroutinefunction(funct):

            @functools.wraps(funct)
            async def async_wrapper(self: _T, *args: _P.args, **kwargs: _P.kwargs) -> Any:
                """Await the call so soco UPnP exceptions surface inside the try block."""
                try:
                    return await funct(self, *args, **kwargs)
                except (OSError, SoCoException, SoCoUPnPException, TimeoutError) as err:
                    _handle_soco_error(self, err, funct.__qualname__, errorcodes)
                    return None

            return cast("_ReturnFuncType[_T, _P, _R]", async_wrapper)

        @functools.wraps(funct)
        def wrapper(self: _T, *args: _P.args, **kwargs: _P.kwargs) -> _R | None:
            """Wrap for all soco UPnP exception."""
            try:
                result = funct(self, *args, **kwargs)
            except (OSError, SoCoException, SoCoUPnPException, TimeoutError) as err:
                _handle_soco_error(self, err, funct.__qualname__, errorcodes)
                return None

            return result

        return wrapper

    return decorator


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
    return soco.visible_zones or set()


def _handle_soco_error(
    instance: Any,
    err: Exception,
    function: str,
    errorcodes: list[str] | None,
) -> None:
    """Ignore a filtered UPnP error code or raise the error as a SonosUpdateError."""
    error_code = getattr(err, "error_code", None)
    if errorcodes and error_code in errorcodes:
        _LOGGER.debug("Error code %s ignored in call to %s", error_code, function)
        return

    if (target := _find_target_identifier(instance)) is None:
        msg = "Unexpected use of soco_error"
        raise RuntimeError(msg) from err

    message = f"Error calling {function} on {target}: {err}"
    raise SonosUpdateError(message) from err


def _find_target_identifier(instance: Any) -> str | None:
    """Return the name or IP address of the speaker, or None if the instance holds no SoCo."""
    if soco := getattr(instance, "soco", None):
        # Only use attributes with no I/O
        return str(soco._player_name or soco.ip_address)
    return None
