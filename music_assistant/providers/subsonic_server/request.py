"""The authenticated request object handed to Subsonic endpoint handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .constants import ErrorCode
from .response import SubsonicError

if TYPE_CHECKING:
    from aiohttp import web
    from multidict import MultiDictProxy
    from music_assistant_models.auth import User


@dataclass(frozen=True)
class SubsonicRequest:
    """An authenticated Subsonic API request."""

    params: MultiDictProxy[str]
    user: User

    def get(self, name: str) -> str | None:
        """Return the first value of a request parameter, if present."""
        return self.params.get(name)

    def get_all(self, name: str) -> list[str]:
        """Return every value of a repeated request parameter."""
        return self.params.getall(name, [])

    def require(self, name: str) -> str:
        """Return the first value of a mandatory request parameter."""
        if (value := self.params.get(name)) is None:
            raise SubsonicError(
                ErrorCode.MISSING_PARAMETER, f"Required parameter is missing: {name}"
            )
        return value

    def get_int(self, name: str, default: int, maximum: int | None = None) -> int:
        """
        Return an integer request parameter, clamped to ``0..maximum``.

        :param name: The parameter name.
        :param default: Value when the parameter is absent.
        :param maximum: Upper bound, if any.
        """
        raw = self.params.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError as err:
            raise SubsonicError(ErrorCode.GENERIC, f"Invalid integer for {name}: {raw}") from err
        value = max(value, 0)
        return min(value, maximum) if maximum is not None else value


type EndpointResult = dict[str, Any] | web.StreamResponse
type EndpointHandler = Callable[[SubsonicRequest], Awaitable[EndpointResult]]
