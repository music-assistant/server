"""Field stabilization for fixture generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.constants import DOMAND_BID_COOKIE_NAME

if TYPE_CHECKING:
    from pydantic import BaseModel, JsonValue

    from tests.providers.nicovideo.types import FixtureAPIResult, JsonDict

# Stabilization constants
DUMMY_COUNT = 1


@dataclass(frozen=True)
class StabilizationInfo:
    """Information about how to stabilize a field."""

    pattern: str
    replacement_value: str | int | float | bool
    is_partial_match: bool = False

    def matches(self, field_name: str) -> bool:
        """Check if this stabilization info matches the given field name."""
        if self.is_partial_match:
            return self.pattern.lower() in field_name.lower()
        return self.pattern == field_name


# Centralized field stabilization rules
STABILIZATION_RULES: list[StabilizationInfo] = [
    # Exact matches
    StabilizationInfo("searchId", "dummy-search-id-for-testing"),
    StabilizationInfo("lastViewedAt", "2025-01-01T00:00:00+09:00"),
    StabilizationInfo("serverTime", "2025-01-01T00:00:00+09:00"),
    StabilizationInfo("registeredAt", "2025-01-01T00:00:00+09:00"),
    StabilizationInfo("nicosid", "dummy_nicosid_for_testing"),
    StabilizationInfo("watchTrackId", "dummy_track_id_for_testing"),
    StabilizationInfo("isPeakTime", False),
    StabilizationInfo(
        "thumbnailUrl", "https://resource.video.nimg.jp/web/img/series/no_thumbnail.png"
    ),
    StabilizationInfo("playbackPosition", 0.0),
    StabilizationInfo("hls_url", "https://dummy.hls.url/for/testing"),
    StabilizationInfo(DOMAND_BID_COOKIE_NAME, "dummy_domand_bid_for_testing"),
    StabilizationInfo("hls_playlist_text", "dummy_hls_playlist_text_for_testing"),
    StabilizationInfo("threadKey", "dummy.jwt.token.for.testing"),
    StabilizationInfo("accessRightKey", "dummy.jwt.token.for.testing"),
    StabilizationInfo("editKey", "dummy.jwt.token.for.testing"),
    StabilizationInfo("views", DUMMY_COUNT),
    # Partial matches
    StabilizationInfo(
        "description", "This is a dummy description for testing purposes.", is_partial_match=True
    ),
]


class FieldStabilizer:
    """Handles stabilization of dynamic fields in fixtures."""

    def __init__(self) -> None:
        """Initialize with stabilization rules."""
        self.rules = STABILIZATION_RULES

    def stabilize[T: BaseModel](self, data: FixtureAPIResult[T]) -> FixtureAPIResult[T]:
        """Stabilize dynamic fields in API responses for consistent fixture generation.

        This function replaces all count-related numeric values with DUMMY_COUNT
        to ensure fixtures are stable across different API response states.
        """
        if isinstance(data, list):
            return [self._stabilize_model(item) for item in data]
        return self._stabilize_model(data)

    def _stabilize_model[T: BaseModel](self, data: T) -> T:
        """Stabilize count values in a single Pydantic model."""
        data_dict = data.model_dump(by_alias=True)
        stabilized_dict = self._stabilize_value("", data_dict, is_in_count_context=False)
        return data.__class__.model_validate(stabilized_dict)

    def _stabilize_value(
        self,
        key: str,
        value: JsonValue,
        is_in_count_context: bool,
    ) -> JsonValue:
        """Stabilize a single value recursively.

        Args:
            key: The field name/key being processed
            value: The value to stabilize
            is_in_count_context: Whether we're inside a count-related field

        Returns:
            Stabilized value
        """
        # 1. Check explicit rules first
        for rule in self.rules:
            if rule.matches(key):
                return rule.replacement_value

        # 2. Handle nested structures
        if isinstance(value, dict):
            return self._stabilize_dict(value, is_in_count_context)
        elif isinstance(value, list):
            return [self._stabilize_value(key, item, is_in_count_context) for item in value]

        # 3. Handle count values
        elif is_in_count_context and isinstance(value, (int, float)):
            return DUMMY_COUNT

        return value

    def _stabilize_dict(self, data: JsonDict, parent_is_count: bool) -> JsonDict:
        """Stabilize all fields in a dictionary.

        Args:
            data: Dictionary to stabilize
            parent_is_count: Whether parent was a count field

        Returns:
            Stabilized dictionary
        """
        return {
            k: self._stabilize_value(k, v, parent_is_count or "count" in k.lower())
            for k, v in data.items()
        }
