"""Model/base for a Plugin Provider implementation."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from mashumaro import field_options, pass_through
from music_assistant_models.enums import StreamType
from music_assistant_models.media_items.audio_format import AudioFormat  # noqa: TC002
from music_assistant_models.player import PlayerMedia, PlayerSource

from .provider import Provider

# ruff: noqa: ARG001, ARG002


@dataclass()
class PluginSource(PlayerSource):
    """
    Model for a PluginSource, which is a player (audio)source provided by a plugin.

    This (intermediate)  model is not exposed on the api,
    but is used internally by the plugin provider.
    """

    # The output format that is sent to the player
    # (or to the library/application that is used to send audio to the player)
    audio_format: AudioFormat | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # metadata of the current playing media (if known)
    metadata: PlayerMedia | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # The type of stream that is provided by this source
    stream_type: StreamType | None = field(
        default=StreamType.CUSTOM,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # The path to the source/audio (if streamtype is not custom)
    path: str | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )
    # in_use_by specifies the player id that is currently using this plugin (if any)
    in_use_by: str | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )


class PluginProvider(Provider):
    """
    Base representation of a Plugin for Music Assistant.

    Plugin Provider implementations should inherit from this base model.
    """

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        # Will only be called if ProviderFeature.AUDIO_SOURCE is declared
        raise NotImplementedError

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """
        Return the (custom) audio stream for the audio source provided by this plugin.

        Will only be called if this plugin is a PluginSource, meaning that
        the ProviderFeature.AUDIO_SOURCE is declared AND if the streamtype is StreamType.CUSTOM.

        The player_id is the id of the player that is requesting the stream.
        """
        yield b""
        raise NotImplementedError
