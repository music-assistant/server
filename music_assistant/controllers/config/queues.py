"""Player queue configuration handling for the ConfigController."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast, overload

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    PlayerQueueConfig,
)
from music_assistant_models.enums import PlaybackState

from music_assistant.constants import CONF_PLAYER_QUEUES, CONF_VALUE_GLOBAL
from music_assistant.controllers.config.constants import (
    PLAYER_QUEUE_CONFIG_OWNER,
    _ConfigValueT,
)
from music_assistant.controllers.config.helpers import _with_translation_owner
from music_assistant.controllers.player_queues.constants import CONF_AUTOPLAY_PLAYLIST
from music_assistant.helpers.api import api_command

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class PlayerQueueConfigMixin:
    """Mixin providing player queue configuration handling for the ConfigController."""

    # Type hints for attributes/methods provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant

        def get(self, key: str, default: Any = None) -> Any: ...  # noqa: D102

        def set(self, key: str, value: Any) -> None: ...  # noqa: D102

    @api_command("config/player_queues", required_scope=Scope.CONFIG_PLAYERS_READ)
    def get_player_queue_configs(self) -> list[PlayerQueueConfig]:
        """Return all (stored) queue configurations."""
        return [
            self._parse_player_queue_config(queue_id, raw_conf)
            for queue_id, raw_conf in list(self.get(CONF_PLAYER_QUEUES, {}).items())
        ]

    @api_command("config/player_queues/get", required_scope=Scope.CONFIG_PLAYERS_READ)
    async def get_player_queue_config_for_api(self, queue_id: str) -> PlayerQueueConfig:
        """Return (full) configuration for a single queue, with dynamic options populated."""
        # The frontend renders the queue settings form straight from this config, so the
        # autoplay playlist dropdown options must be resolved here (the sync core is used by
        # the streaming hot path and must stay free of library lookups).
        config = self.get_player_queue_config(queue_id)
        if (autoplay_playlist := config.values.get(CONF_AUTOPLAY_PLAYLIST)) is not None:
            autoplay_playlist.options = await self._library_playlist_options()
        return config

    def get_player_queue_config(self, queue_id: str) -> PlayerQueueConfig:
        """Return (full) configuration for a single queue."""
        raw_conf = self.get(f"{CONF_PLAYER_QUEUES}/{queue_id}") or {"queue_id": queue_id}
        return self._parse_player_queue_config(queue_id, raw_conf)

    @api_command("config/player_queues/get_entries", required_scope=Scope.CONFIG_PLAYERS_READ)
    async def get_player_queue_config_entries(
        self,
        queue_id: str,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all Config Entries to configure a queue."""
        entries = self.mass.player_queues.get_queue_config_entries(
            playlist_options=await self._library_playlist_options()
        )
        return _with_translation_owner(entries, PLAYER_QUEUE_CONFIG_OWNER)

    @api_command("config/player_queues/get_value", required_scope=Scope.CONFIG_PLAYERS_READ)
    def get_player_queue_config_value(self, queue_id: str, key: str) -> ConfigValueType:
        """Return single config(entry) value for a queue."""
        return self.get_player_queue_config(queue_id).get_value(key)

    if TYPE_CHECKING:

        @overload
        def get_raw_player_queue_config_value(
            self, queue_id: str, key: str, default: _ConfigValueT
        ) -> _ConfigValueT: ...

        @overload
        def get_raw_player_queue_config_value(
            self, queue_id: str, key: str, default: None = None
        ) -> ConfigValueType | None: ...

    def get_raw_player_queue_config_value(
        self, queue_id: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return (raw) single config(entry) value for a queue.

        Returns the stored value as-is (no validation), or the given default when not stored.
        """
        return cast(
            "ConfigValueType",
            self.get(f"{CONF_PLAYER_QUEUES}/{queue_id}/values/{key}", default),
        )

    def get_effective_player_queue_config_value(
        self, queue_id: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return the effective queue config value, following the global (queue controller) value.

        A per-queue value of "global" (or unset) resolves to the matching value on the Player Queues
        core controller, so a queue can either follow the global default or override it — mirroring
        the log_level "GLOBAL" pattern.

        :param queue_id: The queue to read the value for.
        :param key: The config key.
        :param default: The global (queue-controller) default to fall back to when the value is
            stored on neither the queue nor the core config.
        """
        value = self.get_raw_player_queue_config_value(queue_id, key, CONF_VALUE_GLOBAL)
        if value in (CONF_VALUE_GLOBAL, None):
            # self is the ConfigController; go via mass.config for the typed (overloaded) accessor
            return self.mass.config.get_raw_core_config_value(CONF_PLAYER_QUEUES, key, default)
        return value

    @api_command("config/player_queues/save", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def save_player_queue_config(
        self, queue_id: str, values: dict[str, ConfigValueType]
    ) -> PlayerQueueConfig:
        """Save/update PlayerQueueConfig."""
        config = self.get_player_queue_config(queue_id)
        changed_keys = config.update(values)
        conf_key = f"{CONF_PLAYER_QUEUES}/{queue_id}"
        if not changed_keys and self.get(conf_key) is not None:
            # no changes
            return config
        self.set(conf_key, config.to_raw())
        if changed_keys and (queue := self.mass.player_queues.get(queue_id)):
            # refresh derived queue state (e.g. the effective smart-fades indicator) and notify
            # clients so they don't see a stale value until the next unrelated queue update
            queue.smart_fades_active = self.mass.streams.is_smart_fades_active(queue)
            queue.smart_shuffle_active = self.mass.player_queues.is_smart_shuffle_active(queue)
            self.mass.player_queues.signal_update(queue_id)
            # apply immediately: restart playback if a changed setting requires a reload
            requires_restart = any(
                v.requires_reload
                for v in config.values.values()
                if f"values/{v.key}" in changed_keys
            )
            if requires_restart and queue.state == PlaybackState.PLAYING:
                await self.mass.player_queues.stop(queue_id)
                self.mass.call_later(1, self.mass.player_queues.resume, queue_id, False)
        return self.get_player_queue_config(queue_id)

    def _parse_player_queue_config(
        self, queue_id: str, raw_conf: dict[str, Any]
    ) -> PlayerQueueConfig:
        """Parse a (raw) queue config dict into a PlayerQueueConfig with the current entries."""
        raw_conf = {**raw_conf, "queue_id": queue_id}
        entries = self.mass.player_queues.get_queue_config_entries()
        return cast("PlayerQueueConfig", PlayerQueueConfig.parse(entries, raw_conf))

    async def _library_playlist_options(self) -> list[ConfigValueOption]:
        """Return the library playlists as selectable config options (for autoplay playlist mode)."""
        return [
            ConfigValueOption(playlist.uri, title=playlist.name)
            async for playlist in self.mass.music.playlists.iter_library_items()
        ]
