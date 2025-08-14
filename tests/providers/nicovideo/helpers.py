"""Helper functions for nicovideo tests."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar
from unittest.mock import Mock

from pydantic import BaseModel

from tests.providers.nicovideo.constants import (
    DUMMY_COUNT,
    DUMMY_DATETIME,
    DUMMY_DESCRIPTION,
    DUMMY_IS_PEAK_TIME,
    DUMMY_JWT_TOKEN,
    DUMMY_NICOSID,
    DUMMY_PLAYBACK_POSITION,
    DUMMY_SEARCH_ID,
    DUMMY_THUMBNAIL_URL,
    DUMMY_TRACK_ID,
)
from tests.providers.nicovideo.types import FixtureAPIResult, JsonContainer, JsonDict, JsonList

if TYPE_CHECKING:
    from mashumaro import DataClassDictMixin
    from pydantic import JsonValue

from music_assistant.providers.nicovideo.converters.manager import NicovideoConverterManager

T = TypeVar("T")


@dataclass
class StabilizationContext:
    """Context for field stabilization processing."""

    in_count_property: bool = False
    field_path: list[str] = field(default_factory=list)


def create_converter_manager() -> NicovideoConverterManager:
    """Create a NicovideoConverterManager for testing."""
    # Create mock provider
    mock_provider = Mock()
    mock_provider.lookup_key = "nicovideo"
    mock_provider.instance_id = "nicovideo_test"
    mock_provider.domain = "nicovideo"

    # Create mock logger
    mock_logger = Mock()

    return NicovideoConverterManager(mock_provider, mock_logger)


def sort_dict_keys_and_lists(obj: JsonValue) -> JsonValue:
    """Sort dictionary keys and list elements for consistent snapshot comparison.

    This function ensures deterministic ordering by:
    - Sorting dictionary keys alphabetically
    - Sorting list elements by type and string representation

    Particularly useful for handling serialized sets that would otherwise have
    random ordering between test runs.
    """
    if isinstance(obj, dict):
        # Sort dictionary keys and recursively process values
        return {key: sort_dict_keys_and_lists(obj[key]) for key in sorted(obj.keys())}
    elif isinstance(obj, list):
        # Recursively process list items first
        sorted_items = [sort_dict_keys_and_lists(item) for item in obj]
        try:
            # Sort items for deterministic ordering (handles serialized sets)
            return sorted(sorted_items, key=lambda x: (type(x).__name__, str(x)))
        except (TypeError, ValueError):
            # If sorting fails, return in original order
            return sorted_items
    else:
        # Return primitive values as-is
        return obj


def to_dict_for_snapshot(media_item: DataClassDictMixin) -> JsonDict:
    """Convert DataClassDictMixin to dict with sorted keys and lists for snapshot comparison."""
    # Get the standard to_dict representation
    item_dict = media_item.to_dict()

    # Recursively sort all nested structures, especially sets
    sorted_result = sort_dict_keys_and_lists(item_dict)

    # Ensure we return the expected dict type
    if isinstance(sorted_result, dict):
        return sorted_result
    else:
        # This should not happen given the input, but satisfies mypy
        return item_dict


def to_dict_for_fixture[T: BaseModel](response: FixtureAPIResult[T]) -> JsonContainer:
    """Convert response to JSON serializable format."""
    # Check for Pydantic models first
    if isinstance(response, BaseModel):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return response.model_dump(by_alias=True)
    data: JsonList = []
    for item in response:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            data.append(item.model_dump(by_alias=True))
    return data


def stabilize_dynamic_fields_for_fixture[T: BaseModel](
    data: FixtureAPIResult[T],
) -> FixtureAPIResult[T]:
    """Stabilize dynamic fields in API responses for consistent fixture generation.

    This function replaces all count-related numeric values with DUMMY_COUNT
    to ensure fixtures are stable across different API response states.
    """
    if isinstance(data, list):
        return [_stabilize_model_counts(item) for item in data]
    return _stabilize_model_counts(data)


def _stabilize_model_counts[T: BaseModel](data: T) -> T:
    """Stabilize count values in a single Pydantic model."""
    # For Pydantic models, create a copy and update fields
    data_dict = data.model_dump(by_alias=True)
    stabilized_dict = _stabilize_all_fields(data_dict, StabilizationContext())
    return data.__class__.model_validate(stabilized_dict)


def _stabilize_all_fields(data: JsonValue, context: StabilizationContext) -> JsonValue:
    """Stabilize both dynamic fields and count values in a single recursive pass."""
    if isinstance(data, dict):
        stabilized: JsonDict = {}
        for key, value in data.items():
            # Create new context for this field
            new_field_path = [*context.field_path, key]
            is_count_key = "count" in key.lower()
            new_in_count_property = context.in_count_property or is_count_key

            new_context = StabilizationContext(
                in_count_property=new_in_count_property,
                field_path=new_field_path,
            )

            # Stabilize specific dynamic fields by name
            if key == "searchId":
                stabilized[key] = DUMMY_SEARCH_ID
            elif key in ("lastViewedAt", "serverTime", "registeredAt"):
                stabilized[key] = DUMMY_DATETIME
            elif key == "nicosid":
                stabilized[key] = DUMMY_NICOSID
            elif "description" in key.lower():
                stabilized[key] = DUMMY_DESCRIPTION
            elif key == "watchTrackId":
                stabilized[key] = DUMMY_TRACK_ID
            elif key == "isPeakTime":
                stabilized[key] = DUMMY_IS_PEAK_TIME
            elif key == "thumbnailUrl":
                stabilized[key] = DUMMY_THUMBNAIL_URL
            elif key == "playbackPosition":
                stabilized[key] = DUMMY_PLAYBACK_POSITION
            elif key in ("threadKey", "accessRightKey", "editKey"):
                stabilized[key] = DUMMY_JWT_TOKEN
            elif key == "views" and isinstance(value, int):
                stabilized[key] = DUMMY_COUNT
            else:
                # Recursively process nested data with updated context
                stabilized[key] = _stabilize_all_fields(value, new_context)
        return stabilized

    elif isinstance(data, list):
        return [_stabilize_all_fields(item, context) for item in data]

    elif context.in_count_property and isinstance(data, (int, float)):
        # If we're inside a count property, convert all numbers to DUMMY_COUNT
        return DUMMY_COUNT

    # Return other values as-is
    return data
