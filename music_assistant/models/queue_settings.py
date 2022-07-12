"""Model for a PlayerQueue's settings."""
from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any, Dict, Optional

from .enums import ContentType, CrossFadeMode, RepeatMode

if TYPE_CHECKING:
    from .player_queue import PlayerQueue


class QueueSettings:
    """Representation of (user adjustable) PlayerQueue settings/preferences."""

    def __init__(self, queue: PlayerQueue) -> None:
        """Initialize."""
        self._queue = queue
        self.mass = queue.mass
        self._repeat_mode: RepeatMode = RepeatMode.OFF
        self._shuffle_enabled: bool = False
        self._crossfade_mode: CrossFadeMode = CrossFadeMode.DISABLED
        self._crossfade_duration: int = 6
        self._volume_normalization_enabled: bool = True
        self._volume_normalization_target: int = -14
        self._stream_type: ContentType = queue.player.stream_type
        self._max_sample_rate: int = queue.player.max_sample_rate
        self._announce_volume_increase: int = 15

    @property
    def repeat_mode(self) -> RepeatMode:
        """Return repeat enabled setting."""
        return self._repeat_mode

    @repeat_mode.setter
    def repeat_mode(self, enabled: bool) -> None:
        """Set repeat enabled setting."""
        if self._repeat_mode != enabled:
            self._repeat_mode = enabled
            self._on_update("repeat_mode")

    @property
    def shuffle_enabled(self) -> bool:
        """Return shuffle enabled setting."""
        return self._shuffle_enabled

    @shuffle_enabled.setter
    def shuffle_enabled(self, enabled: bool) -> None:
        """Set shuffle enabled setting."""
        if not self._shuffle_enabled and enabled:
            # shuffle requested
            self._shuffle_enabled = True
            cur_index = self._queue.index_in_buffer
            cur_item = self._queue.get_item(cur_index)
            if cur_item is not None:
                played_items = self._queue.items[:cur_index]
                next_items = self._queue.items[cur_index + 1 :]
                # for now we use default python random function
                # can be extended with some more magic based on last_played and stuff
                next_items = random.sample(next_items, len(next_items))

                items = played_items + [cur_item] + next_items
                asyncio.create_task(self._queue.update_items(items))
                self._on_update("shuffle_enabled")
        elif self._shuffle_enabled and not enabled:
            # unshuffle
            self._shuffle_enabled = False
            cur_index = self._queue.index_in_buffer
            cur_item = self._queue.get_item(cur_index)
            if cur_item is not None:
                played_items = self._queue.items[:cur_index]
                next_items = self._queue.items[cur_index + 1 :]
                next_items.sort(key=lambda x: x.sort_index, reverse=False)
                items = played_items + [cur_item] + next_items
                asyncio.create_task(self._queue.update_items(items))
                self._on_update("shuffle_enabled")

    @property
    def crossfade_mode(self) -> CrossFadeMode:
        """Return crossfade mode setting."""
        return self._crossfade_mode

    @crossfade_mode.setter
    def crossfade_mode(self, mode: CrossFadeMode) -> None:
        """Set crossfade enabled setting."""
        if self._crossfade_mode != mode:
            # TODO: restart the queue stream if its playing
            self._crossfade_mode = mode
            self._on_update("crossfade_mode")

    @property
    def crossfade_duration(self) -> int:
        """Return crossfade_duration setting."""
        return self._crossfade_duration

    @crossfade_duration.setter
    def crossfade_duration(self, duration: int) -> None:
        """Set crossfade_duration setting (1..10 seconds)."""
        duration = max(1, duration)
        duration = min(10, duration)
        if self._crossfade_duration != duration:
            self._crossfade_duration = duration
            self._on_update("crossfade_duration")

    @property
    def volume_normalization_enabled(self) -> bool:
        """Return volume_normalization_enabled setting."""
        return self._volume_normalization_enabled

    @volume_normalization_enabled.setter
    def volume_normalization_enabled(self, enabled: bool) -> None:
        """Set volume_normalization_enabled setting."""
        if self._volume_normalization_enabled != enabled:
            self._volume_normalization_enabled = enabled
            self._on_update("volume_normalization_enabled")

    @property
    def volume_normalization_target(self) -> float:
        """Return volume_normalization_target setting."""
        return self._volume_normalization_target

    @volume_normalization_target.setter
    def volume_normalization_target(self, target: float) -> None:
        """Set volume_normalization_target setting (-40..10 LUFS)."""
        target = max(-40, target)
        target = min(10, target)
        if self._volume_normalization_target != target:
            self._volume_normalization_target = target
            self._on_update("volume_normalization_target")

    @property
    def stream_type(self) -> ContentType:
        """Return supported/preferred stream type for this playerqueue."""
        return self._stream_type

    @stream_type.setter
    def stream_type(self, value: ContentType) -> None:
        """Set supported/preferred stream type for this playerqueue."""
        if self._stream_type != value:
            self._stream_type = value
            self._on_update("stream_type")

    @property
    def max_sample_rate(self) -> int:
        """Return max supported/needed sample rate(s) for this playerqueue."""
        return self._max_sample_rate

    @max_sample_rate.setter
    def max_sample_rate(self, value: ContentType) -> None:
        """Set supported/preferred sample rate(s) for this playerqueue."""
        if self._max_sample_rate != value:
            self._max_sample_rate = value
            self._on_update("max_sample_rate")

    @property
    def announce_volume_increase(self) -> int:
        """Return announce_volume_increase setting (percentage relative to current)."""
        return self._announce_volume_increase

    @announce_volume_increase.setter
    def announce_volume_increase(self, volume_increase: int) -> None:
        """Set announce_volume_increase setting."""
        if self._announce_volume_increase != volume_increase:
            self._announce_volume_increase = volume_increase
            self._on_update("announce_volume_increase")

    def to_dict(self) -> Dict[str, Any]:
        """Return dict from settings."""
        return {
            "repeat_mode": self.repeat_mode.value,
            "shuffle_enabled": self.shuffle_enabled,
            "crossfade_mode": self.crossfade_mode.value,
            "crossfade_duration": self.crossfade_duration,
            "volume_normalization_enabled": self.volume_normalization_enabled,
            "volume_normalization_target": self.volume_normalization_target,
            "stream_type": self.stream_type.value,
            "max_sample_rate": self.max_sample_rate,
            "announce_volume_increase": self.announce_volume_increase,
        }

    def from_dict(self, d: Dict[str, Any]) -> None:
        """Initialize settings from dict."""
        self._repeat_mode = RepeatMode(d.get("repeat_mode", self._repeat_mode.value))
        self._shuffle_enabled = bool(d.get("shuffle_enabled", self._shuffle_enabled))
        self._crossfade_mode = CrossFadeMode(
            d.get("crossfade_mode", self._crossfade_mode.value)
        )
        self._crossfade_duration = int(
            d.get("crossfade_duration", self._crossfade_duration)
        )
        self._volume_normalization_enabled = bool(
            d.get("volume_normalization_enabled", self._volume_normalization_enabled)
        )
        self._volume_normalization_target = float(
            d.get("volume_normalization_target", self._volume_normalization_target)
        )
        self._stream_type = ContentType(d.get("stream_type", self._stream_type.value))
        self._max_sample_rate = int(d.get("max_sample_rate", self._max_sample_rate))
        self._announce_volume_increase = int(
            d.get("announce_volume_increase", self._announce_volume_increase)
        )

    async def restore(self) -> None:
        """Restore state from db."""
        values = {}
        for key in self.to_dict():
            db_key = f"{self._queue.queue_id}_{key}"
            if db_value := await self.mass.database.get_setting(db_key):
                values[key] = db_value
        self.from_dict(values)

    def _on_update(self, changed_key: Optional[str] = None) -> None:
        """Handle state changed."""
        self._queue.signal_update()
        self.mass.create_task(self.save(changed_key))

    async def save(self, changed_key: Optional[str] = None) -> None:
        """Save state in db."""
        for key, value in self.to_dict().items():
            if key == changed_key or changed_key is None:
                db_key = f"{self._queue.queue_id}_{key}"
                await self.mass.database.set_setting(db_key, value)
