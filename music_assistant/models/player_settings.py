"""Models and helpers for a player."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from music_assistant.models.enums import EventType, MetadataMode
from music_assistant.models.media_items import ContentType

if TYPE_CHECKING:
    from music_assistant.models.player import Player


class PlayerSettings:
    """Representation of (user adjustable) Player settings/preferences."""

    default_max_sample_rate: int = 96000
    default_stream_type: ContentType = ContentType.FLAC
    default_announce_volume_increase: int = 15

    def __init__(self, player: Player) -> None:
        """Initialize PlayerSettings."""
        self.player = player
        self.mass = player.mass
        self.base_key = f"{player.player_id}.settings"

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
            self._on_update()

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
            self.mass.settings.set(settings_key, value)
            self._on_update()

    @property
    def announce_volume_increase(self) -> int:
        """Return announce_volume_increase setting (percentage relative to current)."""
        settings_key = f"{self.base_key}.announce_volume_increase"
        return self.mass.settings.get(
            settings_key, self.default_announce_volume_increase
        )

    @announce_volume_increase.setter
    def announce_volume_increase(self, value: int) -> None:
        """Set announce_volume_increase setting."""
        cur_value = self.announce_volume_increase
        settings_key = f"{self.base_key}.announce_volume_increase"
        if cur_value != value:
            self.mass.settings.set(settings_key, value)
            self._on_update()

    @property
    def metadata_mode(self) -> MetadataMode:
        """Return metadata mode setting."""
        settings_key = f"{self.base_key}.metadata_mode"
        if val := self.mass.settings.get(settings_key):
            return MetadataMode(val)
        return self.default_stream_type

    @metadata_mode.setter
    def metadata_mode(self, value: MetadataMode) -> None:
        """Set metadata mode setting."""
        cur_value = self.metadata_mode
        settings_key = f"{self.base_key}.metadata_mode"
        if cur_value != value:
            self.mass.settings.set(settings_key, value)
            self._on_update()

    def to_dict(self) -> Dict[str, Any]:
        """Return dict from settings."""
        # return a dict from all property-decorated attributes
        keys = {
            x for x, val in vars(PlayerSettings).items() if isinstance(val, property)
        }
        return {key: getattr(self, key) for key in keys}

    def from_dict(self, d: Dict[str, Any]) -> None:
        """Initialize/change settings from dict."""
        for key, value in d.items():
            setattr(self, key, value)

    def _on_update(self) -> None:
        """Handle state changed."""
        self.mass.signal_event(
            EventType.PLAYER_SETTINGS_UPDATED, self.player.player_id, self
        )
