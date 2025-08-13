"""Helper functions for nicovideo tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar
from unittest.mock import Mock

from tests.providers.nicovideo.constants import DUMMY_COUNT
from tests.providers.nicovideo.types import JsonDict

if TYPE_CHECKING:
    from mashumaro import DataClassDictMixin
    from pydantic import BaseModel, JsonValue

from music_assistant.providers.nicovideo.converters.manager import NicovideoConverterManager

T = TypeVar("T")


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


def sort_dict_keys_and_lists_for_snapshot(obj: JsonValue) -> JsonValue:
    """Sort dictionary keys and list elements for consistent snapshot comparison.

    This function ensures deterministic ordering by:
    - Sorting dictionary keys alphabetically
    - Sorting list elements by type and string representation

    Particularly useful for handling serialized sets that would otherwise have
    random ordering between test runs.
    """
    if isinstance(obj, dict):
        # Sort dictionary keys and recursively process values
        return {key: sort_dict_keys_and_lists_for_snapshot(obj[key]) for key in sorted(obj.keys())}
    elif isinstance(obj, list):
        # Recursively process list items first
        sorted_items = [sort_dict_keys_and_lists_for_snapshot(item) for item in obj]
        try:
            # Sort items for deterministic ordering (handles serialized sets)
            return sorted(sorted_items, key=lambda x: (type(x).__name__, str(x)))
        except (TypeError, ValueError):
            # If sorting fails, return in original order
            return sorted_items
    else:
        # Return primitive values as-is
        return obj


def to_dict_with_sorted_keys_and_lists(media_item: DataClassDictMixin) -> JsonDict:
    """Convert DataClassDictMixin to dict with sorted keys and lists for snapshot comparison.

    This function creates a dictionary representation with deterministic ordering by:
    - Sorting all dictionary keys alphabetically
    - Sorting all list elements by type and string representation

    Particularly useful for ensuring that serialized sets have consistent ordering
    across test runs for reliable snapshot comparison.
    """
    # Get the standard to_dict representation
    item_dict = media_item.to_dict()

    # Recursively sort all nested structures, especially sets
    sorted_result = sort_dict_keys_and_lists_for_snapshot(item_dict)

    # Ensure we return the expected dict type
    if isinstance(sorted_result, dict):
        return sorted_result
    else:
        # This should not happen given the input, but satisfies mypy
        return item_dict


def stabilize_counts_for_fixture[T: BaseModel](data: T | list[T]) -> T | list[T]:
    """Stabilize count values in API responses for consistent fixture generation.

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
    stabilized_dict = _stabilize_count_values(data_dict)
    return data.__class__.model_validate(stabilized_dict)


def _stabilize_count_values(data: JsonValue, in_count_property: bool = False) -> JsonValue:
    """Recursively stabilize count-related values in dictionary data."""
    if isinstance(data, dict):
        stabilized: JsonDict = {}
        for key, value in data.items():
            is_count_key = "count" in key.lower()
            # If it's a count key, mark that we're inside a count property
            new_in_count_property = in_count_property or is_count_key
            stabilized[key] = _stabilize_count_values(value, new_in_count_property)
        return stabilized

    elif isinstance(data, list):
        return [_stabilize_count_values(item, in_count_property) for item in data]

    elif in_count_property and isinstance(data, (int, float)):
        # If we're inside a count property, convert all numbers to DUMMY_COUNT
        return DUMMY_COUNT

    # Return other values as-is
    return data
