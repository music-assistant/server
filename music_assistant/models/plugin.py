"""Model/base for a Plugin Provider implementation."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mashumaro import field_options, pass_through
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.models.player import PlayerSource

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamMetadata


@dataclass
class PluginSource(PlayerSource):
    """
    Model for a PluginSource, which is a player (audio)source provided by a plugin.

    A PluginSource is for example a live audio stream such as a aux/microphone input.

    This (intermediate)  model is not exposed on the api,
    but is used internally by the plugin provider.
    """

    # The PCM audio format provided by this source
    # for realtime audio, we recommend using PCM 16bit 44.1kHz stereo
    audio_format: AudioFormat = field(
        default=AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        ),
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # metadata of the current playing media (if known)
    metadata: StreamMetadata | None = field(
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

    # Optional callbacks for playback control
    # These callbacks will be called by the player controller when control commands are issued
    # and the source reports the corresponding capability (can_play_pause, can_seek, etc.)

    # Callback for play command: async def callback() -> None
    on_play: Callable[[], Awaitable[None]] | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # Callback for pause command: async def callback() -> None
    on_pause: Callable[[], Awaitable[None]] | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # Callback for next track command: async def callback() -> None
    on_next: Callable[[], Awaitable[None]] | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # Callback for previous track command: async def callback() -> None
    on_previous: Callable[[], Awaitable[None]] | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # Callback for seek command: async def callback(position: int) -> None
    on_seek: Callable[[int], Awaitable[None]] | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # Callback for volume change command: async def callback(volume: int) -> None
    on_volume: Callable[[int], Awaitable[None]] | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    # Callback for when this source is selected: async def callback() -> None
    on_select: Callable[[], Awaitable[None]] | None = field(
        default=None,
        compare=False,
        metadata=field_options(serialize="omit", deserialize=pass_through),
        repr=False,
    )

    def as_player_source(self) -> PlayerSource:
        """Return a basic PlayerSource representation without unpicklable callbacks."""
        return PlayerSource(
            id=self.id,
            name=self.name,
            passive=self.passive,
            can_play_pause=self.can_play_pause,
            can_seek=self.can_seek,
            can_next_previous=self.can_next_previous,
        )


class PluginProvider(Provider):
    """
    Base representation of a Plugin for Music Assistant.

    Plugin Provider implementations should inherit from this base model.
    """

    def get_source(self) -> PluginSource:
        """
        Get (audio)source details for this plugin.

        # Will only be called if ProviderFeature.AUDIO_SOURCE is declared
        """
        raise NotImplementedError

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """
        Return the (custom) audio stream for the audio source provided by this plugin.

        Will only be called if this plugin is a PluginSource, meaning that
        the ProviderFeature.AUDIO_SOURCE is declared AND if the streamtype is StreamType.CUSTOM.

        The player_id is the id of the player that is requesting the stream.

        Must return audio data as bytes generator (in the format specified by the audio_format).
        """
        yield b""
        raise NotImplementedError

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        return path
