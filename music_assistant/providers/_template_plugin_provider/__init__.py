"""
DEMO/TEMPLATE Plugin Provider for Music Assistant.

This is an empty plugin provider with no actual implementation.
Its meant to get started developing a new plugin provider for Music Assistant.

Use it as a reference to discover what methods exists and what they should return.
Also it is good to look at existing plugin providers to get a better understanding.

In general, a plugin provider does not have any mandatory implementation details.
It provides additional functionality to Music Assistant and most often it will
interact with the existing core controllers and event logic. For example a Scrobble plugin.

If your plugin needs to communicate with external services or devices, you need to
use a dedicated (async) library for that. You can add these dependencies to the
manifest.json file in the requirements section,
which is a list of (versioned!) python modules (pip syntax) that should be installed
when the provider is selected by the user.

To add a new plugin provider to Music Assistant, you need to create a new folder
in the providers folder with the name of your provider (e.g. 'my_plugin_provider').
In that folder you should create (at least) a __init__.py file and a manifest.json file.

Optional is an icon.svg file that will be used as the icon for the provider in the UI,
but we also support that you specify a material design icon in the manifest.json file.

IMPORTANT NOTE:
We strongly recommend developing on either macOS or Linux and start your development
environment by running the setup.sh scripts in the scripts folder of the repository.
This will create a virtual environment and install all dependencies needed for development.
See also our general DEVELOPMENT.md guide in the repository for more information.

"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, EventType, ProviderFeature
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.models.plugin import PluginProvider, PluginSource

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    # an instance of the provider.
    return MyDemoPluginprovider(mass, manifest, config)


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
    # Config Entries are used to configure the Provider if needed.
    # See the models of ConfigEntry and ConfigValueType for more information what is supported.
    # The ConfigEntry is a dataclass that represents a single configuration entry.
    # The ConfigValueType is an Enum that represents the type of value that
    # can be stored in a ConfigEntry.
    # If your provider does not need any configuration, you can return an empty tuple.
    return ()


class MyDemoPluginprovider(PluginProvider):
    """
    Example/demo Plugin provider.

    Note that this is always subclassed from PluginProvider,
    which in turn is a subclass of the generic Provider model.

    The base implementation already takes care of some convenience methods,
    such as the mass object and the logger. Take a look at the base class
    for more information on what is available.

    Just like with any other subclass, make sure that if you override
    any of the default methods (such as __init__), you call the super() method.
    In most cases its not needed to override any of the builtin methods and you only
    implement the abc methods with your actual implementation.
    """

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        # you should return a set of provider-level features
        # here that your plugin provider supports or empty set if none.
        # at time of writing the only plugin-specific feature is the
        # 'AUDIO_SOURCE' feature which indicates that this provider can
        # provide a (single) audio source to Music Assistant, such as a live stream.
        # we add this feature here to demonstrate the concept.
        return {ProviderFeature.AUDIO_SOURCE}

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called after the provider has been fully loaded into Music Assistant.
        # you can use this for instance to trigger custom (non-mdns) discovery of plugins
        # or any other logic that needs to run after the provider is fully loaded.

        # as reference we will subscribe here to an event on the MA eventbus
        # this is just an example and you can remove this if not needed.
        async def handle_event(event: MassEvent) -> None:
            if event.event == EventType.MEDIA_ITEM_PLAYED:
                # example implementation of handling a media item played event
                self.logger.info("Media item played event received: %s", event.data)

        self.mass.subscribe(handle_event, EventType.MEDIA_ITEM_PLAYED)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is unloaded from Music Assistant.
        # this means also when the provider is getting reloaded

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        # OPTIONAL
        # Will only be called if ProviderFeature.AUDIO_SOURCE is declared
        # you return a PluginSource object that represents the audio source
        # that this plugin provider provides.
        # the audio_format field should be the native audio format of the stream
        # that is returned by the get_audio_stream method.
        return PluginSource(
            id=self.lookup_key,
            name=self.name,
            passive=False,
            can_play_pause=False,
            can_seek=False,
            audio_format=AudioFormat(content_type=ContentType.MP3),
        )

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """
        Return the (custom) audio stream for the audio source provided by this plugin.

        Will only be called if this plugin is a PLuginSource, meaning that
        the ProviderFeature.AUDIO_SOURCE is declared.

        The player_id is the id of the player that is requesting the stream.
        """
        # OPTIONAL
        # Will only be called if ProviderFeature.AUDIO_SOURCE is declared
        # This will be called when this pluginsource has been selected by the user
        # to play on one of the players.

        # you should return an async generator that yields the audio stream data.
        # this is an example implementation that just yields some dummy data
        # you should replace this with your actual implementation.
        for _ in range(100):
            yield b"dummy audio data"
