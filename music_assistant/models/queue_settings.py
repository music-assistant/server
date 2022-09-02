"""Model for a PlayerQueue's settings."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from .enums import CrossFadeMode, EventType, RepeatMode

if TYPE_CHECKING:
    from music_assistant.models.player_queue import PlayerQueue


class QueueSettings:
    """Representation of (user adjustable) PlayerQueue settings/preferences."""

    default_repeat_mode: RepeatMode = RepeatMode.OFF
    default_shuffle_enabled: bool = False
    default_crossfade_mode: CrossFadeMode = CrossFadeMode.DISABLED
    default_crossfade_duration: int = 6
    default_volume_normalization_enabled: bool = True
    default_volume_normalization_target: int = -14

    def __init__(self, queue: PlayerQueue) -> None:
        """Initialize PlayerSettings."""
        self.mass = queue.mass
        self.queue = queue
        self.base_key = f"{queue.queue_id}.settings"

    @property
    def repeat_mode(self) -> RepeatMode:
        """Return repeat enabled setting."""
        settings_key = f"{self.base_key}.repeat_mode"
        if val := self.mass.settings.get(settings_key):
            return RepeatMode(val)
        return self.default_repeat_mode

    @repeat_mode.setter
    def repeat_mode(self, value: RepeatMode) -> None:
        """Set repeat enabled setting."""
        cur_value = self.repeat_mode
        settings_key = f"{self.base_key}.repeat_mode"
        if cur_value != value:
            self.mass.settings.set(settings_key, value)
            self._on_update()

    @property
    def shuffle_enabled(self) -> bool:
        """Return shuffle enabled setting."""
        settings_key = f"{self.base_key}.shuffle_enabled"
        if val := self.mass.settings.get(settings_key):
            return bool(val)
        return self.default_shuffle_enabled

    @shuffle_enabled.setter
    def shuffle_enabled(self, value: bool) -> None:
        """Set shuffle enabled setting."""
        cur_value = self.shuffle_enabled
        settings_key = f"{self.base_key}.shuffle_enabled"
        if cur_value == value:
            return
        self.mass.settings.set(settings_key, value)
        queue = self.mass.players.get_player(self.queue.queue_id).queue
        cur_index = queue.index_in_buffer or queue.current_index or 0
        next_index = cur_index + 1
        next_items = queue.items[next_index:]

        if not value:
            # shuffle disabled, try to restore original sort order
            next_items.sort(key=lambda x: x.sort_index, reverse=False)
        self.mass.create_task(
            queue.load(
                next_items,
                insert_at_index=next_index,
                keep_remaining=False,
                shuffle=value,
            )
        )
        self._on_update()

    @property
    def crossfade_mode(self) -> CrossFadeMode:
        """Return crossfade mode setting."""
        settings_key = f"{self.base_key}.crossfade_mode"
        if val := self.mass.settings.get(settings_key):
            return CrossFadeMode(val)
        return self.default_crossfade_mode

    @crossfade_mode.setter
    def crossfade_mode(self, value: CrossFadeMode) -> None:
        """Set crossfade enabled setting."""
        cur_value = self.repeat_mode
        settings_key = f"{self.base_key}.crossfade_mode"
        if cur_value != value:
            self.mass.settings.set(settings_key, value)
            self._on_update()

    @property
    def crossfade_duration(self) -> int:
        """Return crossfade_duration setting."""
        settings_key = f"{self.base_key}.crossfade_duration"
        if val := self.mass.settings.get(settings_key):
            return int(val)
        return self.default_crossfade_duration

    @crossfade_duration.setter
    def crossfade_duration(self, value: int) -> None:
        """Set crossfade_duration setting (1..10 seconds)."""
        value = max(1, value)
        value = min(10, value)
        cur_value = self.crossfade_duration
        settings_key = f"{self.base_key}.crossfade_duration"
        if cur_value != value:
            self.mass.settings.set(settings_key, value)
            self._on_update()

    @property
    def volume_normalization_enabled(self) -> bool:
        """Return volume_normalization_enabled setting."""
        settings_key = f"{self.base_key}.volume_normalization_enabled"
        if val := self.mass.settings.get(settings_key):
            return bool(val)
        return self.default_volume_normalization_enabled

    @volume_normalization_enabled.setter
    def volume_normalization_enabled(self, value: bool) -> None:
        """Set volume_normalization_enabled setting."""
        cur_value = self.volume_normalization_enabled
        settings_key = f"{self.base_key}.volume_normalization_enabled"
        if cur_value != value:
            self.mass.settings.set(settings_key, value)
            self._on_update()

    @property
    def volume_normalization_target(self) -> float:
        """Return volume_normalization_target setting."""
        settings_key = f"{self.base_key}.volume_normalization_target"
        if val := self.mass.settings.get(settings_key):
            return int(val)
        return self.default_volume_normalization_target

    @volume_normalization_target.setter
    def volume_normalization_target(self, value: float) -> None:
        """Set volume_normalization_target setting (-40..10 LUFS)."""
        value = max(-40, value)
        value = min(10, value)
        cur_value = self.volume_normalization_target
        settings_key = f"{self.base_key}.volume_normalization_target"
        if cur_value != value:
            self.mass.settings.set(settings_key, value)
            self._on_update()

    def to_dict(self) -> Dict[str, Any]:
        """Return dict from settings."""
        # return a dict from all property-decorated attributes
        keys = {
            x for x, val in vars(QueueSettings).items() if isinstance(val, property)
        }
        return {key: getattr(self, key) for key in keys}

    def from_dict(self, d: Dict[str, Any]) -> None:
        """Initialize/change settings from dict."""
        for key, value in d.items():
            setattr(self, key, value)

    def _on_update(self) -> None:
        """Handle state changed."""
        self.mass.signal_event(
            EventType.QUEUE_SETTINGS_UPDATED, self.queue.queue_id, self
        )
