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
    return Dummy(mass, manifest, config)


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

    def __init__(
        self,
        provider: PlayerProvider,
        player_id: str,
    ) -> None:
        """Init MC Player.

        Keep reference to physical and zone device.
        """
        # AttributeError: 'DummyPlayer' object has no attribute '_cache'
        self._cache: dict[str, Any] = {}  # comment, then ^

        super().__init__(provider, player_id)

    async def setup(self) -> None:
        """Set up player in Music Assistant."""
        self.set_static_properties()

        self._attr_powered = True
        self._attr_volume_muted = True
        self._attr_name = "foo"

        self._attr_playback_state = PlaybackState.IDLE

        self.update_state()  #  dataclasses.FrozenInstanceError: cannot assign to field 'name'
        await self.mass.players.register_or_update(self)  # RuntimeError: Invalid provider ID given:

    def set_static_properties(self) -> None:
        """Set static properties."""
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.POWER,
        }

        # polling
        self._attr_needs_poll = True
        self._attr_poll_interval = 10

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


class Dummy(PlayerProvider):
    """MusicCast Player Provider."""

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return set()

    async def handle_async_init(self) -> None:
        """Async init."""
        # make some players here
        for i in range(4):
            player = DummyPlayer(self, f"{i}")
            await player.setup()
