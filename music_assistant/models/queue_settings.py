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
        self._volume_normalization_target: int = -23

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
            if self._queue.current_index is not None:
                played_items = self._queue.items[: self._queue.current_index]
                next_items = self._queue.items[self._queue.current_index + 1 :]
                # for now we use default python random function
                # can be extended with some more magic based on last_played and stuff
                next_items = random.sample(next_items, len(next_items))
                items = played_items + [self._queue.current_item] + next_items
                asyncio.create_task(self._queue.update(items))
                self._on_update("shuffle_enabled")
        elif self._shuffle_enabled and not enabled:
            # unshuffle
            self._shuffle_enabled = False
            if self._queue.current_index is not None:
                played_items = self._queue.items[: self._queue.current_index]
                next_items = self._queue.items[self._queue.current_index + 1 :]
                next_items.sort(key=lambda x: x.sort_index, reverse=False)
                items = played_items + [self._queue.current_item] + next_items
                asyncio.create_task(self._queue.update(items))
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
        """Return supported/preferred stream type for playerqueue. Read only."""
        # determine default stream type from player capabilities
        return next(
            x
            for x in (
                ContentType.FLAC,
                ContentType.WAV,
                ContentType.PCM_S16LE,
                ContentType.MP3,
                ContentType.MPEG,
            )
            if x in self._queue.player.supported_content_types
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return dict from settings."""
        return {
            "repeat_mode": self.repeat_mode.value,
            "shuffle_enabled": self.shuffle_enabled,
            "crossfade_mode": self.crossfade_mode.value,
            "crossfade_duration": self.crossfade_duration,
            "volume_normalization_enabled": self.volume_normalization_enabled,
            "volume_normalization_target": self.volume_normalization_target,
        }

    async def restore(self) -> None:
        """Restore state from db."""
        async with self.mass.database.get_db() as _db:
            for key, val_type in (
                ("repeat_mode", RepeatMode),
                ("crossfade_mode", CrossFadeMode),
                ("shuffle_enabled", bool),
                ("crossfade_duration", int),
                ("volume_normalization_enabled", bool),
                ("volume_normalization_target", float),
            ):
                db_key = f"{self._queue.queue_id}_{key}"
                if db_value := await self.mass.database.get_setting(db_key, db=_db):
                    value = val_type(db_value["value"])
                    setattr(self, f"_{key}", value)

    def _on_update(self, changed_key: Optional[str] = None) -> None:
        """Handle state changed."""
        self._queue.signal_update()
        self.mass.create_task(self.save(changed_key))
        # TODO: restart play if setting changed that impacts playing queue

    async def save(self, changed_key: Optional[str] = None) -> None:
        """Save state in db."""
        async with self.mass.database.get_db() as _db:
            for key, value in self.to_dict().items():
                if key == changed_key or changed_key is None:
                    db_key = f"{self._queue.queue_id}_{key}"
                    await self.mass.database.set_setting(db_key, value, db=_db)
