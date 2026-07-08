"""DSP configuration handling for the ConfigController."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import shortuuid
from music_assistant_models.auth import Scope
from music_assistant_models.dsp import DSPConfig, DSPConfigPreset
from music_assistant_models.enums import EventType

from music_assistant.constants import CONF_PLAYER_DSP, CONF_PLAYER_DSP_PRESETS
from music_assistant.helpers.api import api_command

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class DSPConfigMixin:
    """Mixin providing DSP configuration handling for the ConfigController."""

    # Type hints for attributes/methods provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant

        def get(self, key: str, default: Any = None) -> Any: ...  # noqa: D102

        def set(self, key: str, value: Any) -> None: ...  # noqa: D102

    @api_command("config/players/dsp/get", required_scope=Scope.CONFIG_PLAYERS_READ)
    def get_player_dsp_config(self, player_id: str) -> DSPConfig:
        """
        Return the DSP Configuration for a player.

        In case the player does not have a DSP configuration, a default one is returned.
        """
        if raw_conf := self.get(f"{CONF_PLAYER_DSP}/{player_id}"):
            return DSPConfig.from_dict(raw_conf)
        # return default DSP config
        dsp_config = DSPConfig()
        # The DSP config does not do anything by default, so we disable it
        dsp_config.enabled = False
        return dsp_config

    @api_command("config/players/dsp/save", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def save_dsp_config(self, player_id: str, config: DSPConfig) -> DSPConfig:
        """
        Save/update DSPConfig for a player.

        This method will validate the config and apply it to the player.
        """
        # validate the new config
        config.validate()

        old_dsp_enabled = self.get_player_dsp_config(player_id).enabled
        # Save and apply the new config to the player
        self.set(f"{CONF_PLAYER_DSP}/{player_id}", config.to_dict())
        if old_dsp_enabled or config.enabled:
            await self.mass.players.on_player_dsp_change(player_id)
        # send the dsp config updated event
        self.mass.signal_event(
            EventType.PLAYER_DSP_CONFIG_UPDATED,
            object_id=player_id,
            data=config,
        )
        return config

    @api_command("config/dsp_presets/get", required_scope=Scope.CONFIG_PLAYERS_READ)
    async def get_dsp_presets(self) -> list[DSPConfigPreset]:
        """Return all user-defined DSP presets."""
        raw_presets = self.get(CONF_PLAYER_DSP_PRESETS, {})
        return [DSPConfigPreset.from_dict(preset) for preset in raw_presets.values()]

    @api_command("config/dsp_presets/save", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def save_dsp_presets(self, preset: DSPConfigPreset) -> DSPConfigPreset:
        """
        Save/update a user-defined DSP presets.

        This method will validate the config before saving it to the persistent storage.
        """
        preset.validate()

        if preset.preset_id is None:
            # Generate a new preset_id if it does not exist
            preset.preset_id = shortuuid.random(8).lower()

        # Save the preset to the persistent storage
        self.set(f"{CONF_PLAYER_DSP_PRESETS}/preset_{preset.preset_id}", preset.to_dict())

        all_presets = await self.get_dsp_presets()

        self.mass.signal_event(
            EventType.DSP_PRESETS_UPDATED,
            data=all_presets,
        )

        return preset

    @api_command("config/dsp_presets/remove", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def remove_dsp_preset(self, preset_id: str) -> None:
        """Remove a user-defined DSP preset."""
        self.mass.config.remove(f"{CONF_PLAYER_DSP_PRESETS}/preset_{preset_id}")

        all_presets = await self.get_dsp_presets()

        self.mass.signal_event(
            EventType.DSP_PRESETS_UPDATED,
            data=all_presets,
        )
