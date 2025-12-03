"""Type definitions for nicovideo tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import JsonValue

# JSON value type alias for better type safety
type JsonDict = dict[str, JsonValue]
type JsonList = list[JsonValue]
type JsonContainer = JsonDict | JsonList
