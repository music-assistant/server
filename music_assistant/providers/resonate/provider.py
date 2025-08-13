"""Resonate Player Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from aioresonate.server import ResonateServer
from music_assistant_models.enums import ProviderFeature
from zeroconf import ServiceStateChange

from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from zeroconf.asyncio import AsyncServiceInfo


class ResonatePlayerprovider(PlayerProvider):
    """
    Example/demo Player provider.

    Note that this is always subclassed from PlayerProvider,
    which in turn is a subclass of the generic Provider model.

    The base implementation already takes care of some convenience methods,
    such as the mass object and the logger. Take a look at the base class
    for more information on what is available.

    Just like with any other subclass, make sure that if you override
    any of the default methods (such as __init__), you call the super() method.
    In most cases its not needed to override any of the builtin methods and you only
    implement the abc methods with your actual implementation.
    """

    server: ResonateServer

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize a new Resonate player provider."""
        super().__init__(mass, manifest, config)
        self.server = ResonateServer(self.mass.loop)

    @property
    @override
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        # MANDATORY
        # you should return a set of provider-level (optional) features
        # here that your player provider supports or an empty set if none.
        # for example 'ProviderFeature.SYNC_PLAYERS' if you can sync players.
        return {ProviderFeature.SYNC_PLAYERS}

    @override
    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is initialized in Music Assistant.
        # you can use this to do any async initialization of the provider,
        # such as loading configuration, setting up connections, etc.
        self.logger.info("Initializing DemoPlayerProvider with config: %s", self.config)

    @override
    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called after the provider has been fully loaded into Music Assistant.
        # you can use this for instance to trigger custom (non-mdns) discovery of players
        # or any other logic that needs to run after the provider is fully loaded.
        self.logger.info("DemoPlayerProvider loaded")
        await self.discover_players()

    @override
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
        for player in self.players:
            # if you have any cleanup logic for the players, you can do that here.
            # e.g. disconnecting from the player, closing connections, etc.
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    @override
    def on_player_enabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets enabled."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # you want to do something special when a player is enabled.

    @override
    def on_player_disabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets disabled."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # you want to do something special when a player is disabled.
        # e.g. you can stop polling the player or disconnect from it.

    @override
    async def remove_player(self, player_id: str) -> None:
        """Remove a player from this provider."""
        # OPTIONAL - required only if you specified ProviderFeature.REMOVE_PLAYER
        # this is used to actually remove a player.

    @override
    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if state_change == ServiceStateChange.Removed:
            # we don't listen for removed players here.
            # instead we just wait for the player connection to fail
            return
        if not info:
            return  # guard

        name = name.split("@", 1)[1] if "@" in name else name
        if path := info.properties.get(b"path"):
            ip = get_primary_ip_address_from_zeroconf(info)
            assert ip
            url = "ws://" + ip + ":" + str(get_port_from_zeroconf(info)) + path.decode()

            self.logger.debug("Discovered resonate player, connecting to %s", url)
            _ = await self.server.connect_to_player(url)
        # player_id = info.decoded_properties["player_id"]
        # TODO add player discovery handling here
