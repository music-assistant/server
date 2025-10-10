"""Type definitions for nicovideo tests."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeGuard, get_args

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel, JsonValue

FixtureCategory = Literal["tracks", "playlists", "albums", "artists", "search", "history", "stream"]


def is_fixture_category(
    string: str,
) -> TypeGuard[FixtureCategory]:
    """Check if string is a valid fixture category."""
    valid_categories = get_args(FixtureCategory)
    return string in valid_categories


type FixtureAPIResultBase[R: BaseModel, Defaults] = R | list[R] | Defaults

type FixtureAPIResult[R: BaseModel] = FixtureAPIResultBase[R, R]
type FixtureAPIResultOptional[R: BaseModel] = FixtureAPIResultBase[R, None]


# JSON value type alias for better type safety
type JsonDict = dict[str, JsonValue]
type JsonList = list[JsonValue]
type JsonContainer = JsonDict | JsonList


class FixtureProcessorProtocol(Protocol):
    """Protocol for fixture processing operations.

    Defines minimal interface for fixture generation, allowing components
    to depend only on process_fixture rather than full orchestrator implementation.
    This enables loose coupling and alternative implementations.
    """

    async def process_fixture[T: BaseModel, **P](
        self,
        category: FixtureCategory,
        name: str,
        api_call: Callable[
            P, FixtureAPIResultOptional[T] | Coroutine[Any, Any, FixtureAPIResultOptional[T]]
        ],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> FixtureAPIResultOptional[T]:
        """Save API response as fixture and return the data."""
        ...
