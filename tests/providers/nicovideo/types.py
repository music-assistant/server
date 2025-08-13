"""Type definitions for nicovideo tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypeGuard, get_args

if TYPE_CHECKING:
    from pydantic import BaseModel, JsonValue

FixtureCategory = Literal["tracks", "playlists", "albums", "artists", "search", "history"]


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
