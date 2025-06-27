"""MusicCast for MusicAssistant."""

from typing import Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import PlaybackState, PlayerFeature, ProviderFeature
from music_assistant_models.player import PlayerMedia
from music_assistant_models.provider import ProviderManifest

from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return DummyProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return ()


class DummyPlayer(Player):
    """DummyPlayer in Music Assistant."""

    async def setup(self) -> None:
        """Set up player in Music Assistant."""
        self._set_static_properties()
        self.update_state()  #  dataclasses.FrozenInstanceError: cannot assign to field 'name'
        await self.mass.players.register_or_update(self)  # RuntimeError: Invalid provider ID given:

    def update_state(self, force_update=False):
        """
        Update the PlayerState with the current state of the player.

        This method should be called to update the player's state
        and signal any changes to the PlayerController.

        :param force_update: If True, a state update event will be
        pushed even if the state has not actually changed.
        """
        self._update_attributes()
        return super().update_state(force_update)

    def _set_static_properties(self) -> None:
        """Set static properties of the player."""
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.POWER,
        }
        self._attr_needs_poll = True
        self._attr_poll_interval = 10

    def _update_attributes(self) -> None:
        """Update/set (dynamic) properties."""
        self._attr_powered = True
        self._attr_volume_muted = True
        self._attr_name = "foo"
        self._attr_playback_state = PlaybackState.IDLE

    async def power(self, powered: bool) -> None:
        """Power command."""

    async def volume_set(self, volume_level: int) -> None:
        """Volume set command."""

    async def volume_mute(self, muted: bool) -> None:
        """Volume mute command."""

    async def play(self) -> None:
        """Play command."""

    async def stop(self) -> None:
        """Stop command."""

    async def pause(self) -> None:
        """Pause command."""

    async def next_track(self) -> None:
        """Next command."""

    async def previous_track(self) -> None:
        """Previous command."""

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""

    async def poll(self) -> None:
        """Poll player."""


class DummyProvider(PlayerProvider):
    """MusicCast Player Provider."""

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return set()

    async def handle_async_init(self) -> None:
        """Async init."""
        # make some players here
        for i in range(4):
            player = DummyPlayer(self, f"dummy_{i}")
            await player.setup()
