"""Models and helpers for a player."""
from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.models.media_items import ContentType

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class PlayerSettings:
    """Representation of (user adjustable) Player settings/preferences."""

    default_max_sample_rate: int = 96000
    default_stream_type: ContentType = ContentType.FLAC

    def __init__(self, mass: MusicAssistant, player_id: str) -> None:
        """Initialize PlayerSettings."""
        self.mass = mass
        self.player_id = player_id
        self.base_key = f"{self.player_id}.settings"

    @property
    def max_sample_rate(self) -> int:
        """Return the (default) max supported sample rate."""
        # if a player does not report/set its supported sample rates, we use a pretty safe default
        settings_key = f"{self.base_key}.max_sample_rate"
        return self.mass.settings.get(settings_key, self.default_max_sample_rate)

    @max_sample_rate.setter
    def max_sample_rate(self, value: int) -> None:
        """Set the (default) max supported sample rate."""
        cur_value = self.max_sample_rate
        settings_key = f"{self.base_key}.max_sample_rate"
        if cur_value != value:
            self.mass.settings.set(settings_key, value)

    @property
    def stream_type(self) -> ContentType:
        """Return the default/preferred content type to use for streaming."""
        settings_key = f"{self.base_key}.stream_type"
        if val := self.mass.settings.get(settings_key):
            return ContentType(val)
        return self.default_stream_type

    @stream_type.setter
    def stream_type(self, value: ContentType) -> None:
        """Set the default/preferred content type to use for streaming."""
        cur_value = self.stream_type
        settings_key = f"{self.base_key}.stream_type"
        if cur_value != value:
            self.mass.settings.set(settings_key, value.value)
