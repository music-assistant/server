"""
DEMO/TEST/DUMMY/TEMPLATE Player Provider for Music Assistant.

This is an empty player provider with a test/demo implementation.
Its meant to get started developing a new player provider for Music Assistant.

Use it as a reference to discover what methods exists and what they should return.
Also it is good to look at existing player providers to get a better understanding,
due to the fact that providers may be flexible and support different features and/or
ways to discover players on the network.

In general, the actual device communication should reside in a separate library.
You can then reference your library in the manifest in the requirements section,
which is a list of (versioned!) python modules (pip syntax) that should be installed
when the provider is selected by the user.

To add a new player provider to Music Assistant, you need to create a new folder
in the providers folder with the name of your provider (e.g. 'my_player_provider').
In that folder you should create (at least) a __init__.py file and a manifest.json file.

Optional is an icon.svg file that will be used as the icon for the provider in the UI,
but we also support that you specify a material design icon in the manifest.json file.

IMPORTANT NOTE:
We strongly recommend developing on either macOS or Linux and start your development
environment by running the setup.sh scripts in the scripts folder of the repository.
This will create a virtual environment and install all dependencies needed for development.

For all development instructions, please refer to the developer documentation:
https://developers.music-assistant.io
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import DemoPlayerprovider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    # MANDATORY
    # this constant should contain a set of provider-level features
    # that your provider supports or an empty set if none.
    # see the ProviderFeature enum for all available features
    ProviderFeature.SYNC_PLAYERS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    # an instance of your provider.
    return DemoPlayerprovider(mass, manifest, config, SUPPORTED_FEATURES)
