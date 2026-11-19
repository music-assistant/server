"""Model/base for a Plugin Provider implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import UnsupportedFeaturedException

from .media_capabilities import (
    AudioSourceMixin,
    MediaCatalogMixin,
    MusicDiscoveryMixin,
    RecommendationsMixin,
)
from .provider import Provider

if TYPE_CHECKING:
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.streamdetails import StreamDetails


# separator between the owning provider's instance_id and the provider-scoped engine id;
# occurs in neither MA instance_ids nor Home Assistant entity_ids
ENGINE_UID_SEPARATOR = "/"


@dataclass(kw_only=True)
class PluginEngine:
    """
    A single selectable backend exposed by a plugin provider.

    One plugin can expose several engines (for example one per Home Assistant entity),
    so consumers offer them as options in a config picker rather than treating the
    plugin itself as the unit of choice. The chosen engine is stored in config by its
    ``uid`` and handed back to the owning provider as the provider-scoped ``id``.

    Server-side only: never serialized to clients.
    """

    id: str
    name: str
    provider: PluginProvider

    @property
    def uid(self) -> str:
        """Return the globally unique id for this engine, as stored in config."""
        return f"{self.provider.instance_id}{ENGINE_UID_SEPARATOR}{self.id}"


@dataclass(kw_only=True)
class AIEngine(PluginEngine):
    """An engine that answers AI queries, invoked through ``PluginProvider.ai_query``."""


@dataclass(kw_only=True)
class TTSEngine(PluginEngine):
    """An engine that renders speech, invoked through ``PluginProvider.get_tts_message``."""


@dataclass(kw_only=True)
class ImageEngine(PluginEngine):
    """An engine that generates images, invoked through ``PluginProvider.ai_generate_image``."""


class PluginProvider(
    MediaCatalogMixin, RecommendationsMixin, MusicDiscoveryMixin, AudioSourceMixin, Provider
):
    """
    Base representation of a Plugin for Music Assistant.

    Plugin Provider implementations should inherit from this base model.
    """

    async def on_media_item_played(self, report: MediaItemPlaybackProgressReport) -> None:
        """
        Record a playback progress report of a media item.

        Will only be called if ProviderFeature.SCROBBLE is declared. Fired for every queue,
        periodically while an item plays and whenever its playback state or the current
        item changes, so also on pause, when it ends or when it is skipped;
        ``report.is_playing`` and ``report.fully_played`` tell those apart. May fire before
        ``loaded_in_mass`` ran, so ignore reports until everything the plugin needs is set
        up. The report names the playing user and player, so a plugin recording for some of
        them only filters on those. ``ScrobblerHelper`` (helpers/scrobbler.py) builds the
        usual now-playing and scrobble handling on top of this hook.

        :param report: The playback progress report of the played item.
        """
        if ProviderFeature.SCROBBLE in self.supported_features:
            raise NotImplementedError

    async def get_tts_engines(self) -> list[TTSEngine]:
        """
        Return the TTS engines this plugin exposes.

        Will only be called if ProviderFeature.TTS is declared.

        May change over time (e.g. when the backend adds or removes voices/entities).
        The user picks one of these in the config of a consuming provider.

        :return: A list of TTSEngine items. Return an empty list if the plugin
            currently has no engines to expose (e.g. the backend is offline).
        """
        if ProviderFeature.TTS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_tts_message(
        self,
        message: str,
        language: str | None = None,
        engine_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> StreamDetails:
        """
        Convert text to speech audio.

        Will only be called if ProviderFeature.TTS is declared.

        :param message: The text to convert to speech.
        :param language: Optional language code.
        :param engine_id: The provider-scoped id of the engine to use (``TTSEngine.id``,
            not its ``uid``). Omit or pass None to use the plugin's own default engine.
        :param options: Optional integration-specific options (for example a voice
            tuning parameter), passed through to the engine as-is. Ignored by plugins
            that have none.
        :return: StreamDetails for the generated audio. ``path`` must be either a
            fetchable http(s)/rtsp/rtmp URL or the absolute path of an existing local
            file, and must stay resolvable for as long as consumers may play the clip.
        """
        raise NotImplementedError

    async def get_ai_engines(self) -> list[AIEngine]:
        """
        Return the AI engines this plugin exposes.

        Will only be called if ProviderFeature.AI_QUERY is declared.

        May change over time (e.g. when the backend adds or removes entities).
        The user picks one of these in the config of a consuming provider.

        :return: A list of AIEngine items. Return an empty list if the plugin
            currently has no engines to expose (e.g. the backend is offline).
        """
        if ProviderFeature.AI_QUERY in self.supported_features:
            raise NotImplementedError
        return []

    async def ai_query(self, query: str, engine_id: str | None = None) -> str:
        """
        Handle an AI query.

        Will only be called if ProviderFeature.AI_QUERY is declared.

        :param query: The query/prompt to send.
        :param engine_id: The provider-scoped id of the engine to use (``AIEngine.id``,
            not its ``uid``). Omit or pass None to use the plugin's own default engine.
        :return: The AI response as a string.
        """
        raise NotImplementedError

    async def get_image_engines(self) -> list[ImageEngine]:
        """
        Return the image generation engines this plugin exposes.

        Will only be called if ProviderFeature.AI_QUERY is declared. Unlike the AI engines this
        is optional: a plugin that can not generate images keeps the default empty list.

        May change over time (e.g. when the backend adds or removes entities).
        The user picks one of these in the config of a consuming provider.

        :return: A list of ImageEngine items. Return an empty list if the plugin
            currently has no engines to expose (e.g. the backend is offline).
        """
        return []

    async def ai_generate_image(self, prompt: str, engine_id: str | None = None) -> bytes:
        """
        Generate an image from a text prompt.

        Will only be called for a plugin that exposes image engines.

        :param prompt: The description of the image to generate.
        :param engine_id: The provider-scoped id of the engine to use (``ImageEngine.id``,
            not its ``uid``). Omit or pass None to use the plugin's own default engine.
        :return: The encoded image (for example PNG or JPEG) as raw bytes.
        """
        msg = f"{self.name} can not generate images"
        raise UnsupportedFeaturedException(msg)
