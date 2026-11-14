"""Demo Player Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from zeroconf import ServiceStateChange

from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_NUMBER_OF_PLAYERS
from .player import DemoPlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


class DemoPlayerprovider(PlayerProvider):
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

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for this (existing) provider instance.

        Return an empty tuple when the provider has no options. Interactive setup input
        (if any) is collected by a ``setup_flow.py`` module; one-shot buttons are declared
        here as ``ConfigEntryType.ACTION`` entries and handled in ``handle_config_action``.
        """
        return (
            # example of a ConfigEntry for the number of players to create
            ConfigEntry(
                key=CONF_NUMBER_OF_PLAYERS,
                type=ConfigEntryType.INTEGER,
                label="Number of Players",
                required=True,
                default_value=2,
                description="Number of demo players to create.",
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is initialized in Music Assistant.
        # you can use this to do any async initialization of the provider,
        # such as loading configuration, setting up connections, etc.
        self.logger.info("Initializing DemoPlayerProvider with config: %s", self.config)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called after the provider has been fully loaded into Music Assistant.
        self.logger.info("DemoPlayerProvider loaded")

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
        for protocol in self.protocols:
            # if you have any cleanup logic for your devices, you can do that here.
            # e.g. disconnecting from the device, closing connections, etc.
            # The player controller detaches the protocols of an unloading provider itself.
            self.logger.debug("Unloading %s", protocol.display_name)

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        # MANDATORY IF YOU WANT TO USE MDNS DISCOVERY
        # OPTIONAL if you dont use mdns for discovery of players
        # If you specify a mdns service type in the manifest.json, this method will be called
        # automatically on mdns changes for the specified service type.

        # If no mdns service type is specified, this method is omitted and you
        # can completely remove it from your provider implementation.

        if not info:
            return  # guard

        # NOTE: If you do not use mdns for discovery of players on the network,
        # you must implement your own discovery mechanism and logic to add new players
        # and update them on state changes when needed.
        # Below is a bit of example implementation but we advise to look at existing
        # player providers for more inspiration.
        name = name.split("@", 1)[1] if "@" in name else name
        protocol_id = info.decoded_properties["uuid"]  # this is just an example!
        if not protocol_id:
            return  # guard, we need an id for the protocol object of the device

        # handle removed device
        if state_change == ServiceStateChange.Removed:
            # check if there is an attached protocol object for this device
            # note: look up your own protocol objects through the provider,
            # mass.players.get_player is meant for the user-facing player (its state).
            if protocol := self.get_protocol(protocol_id):
                # the device has become unavailable: its player stays, without this protocol
                self.logger.debug("Device offline: %s", protocol.display_name)
                await self.mass.players.unregister(protocol_id)
            return
        # handle update for existing device
        # (state change is either updated or added)
        cur_address = get_primary_ip_address_from_zeroconf(info)
        # check if we have an attached protocol object for this device
        # note that you can use this point to update the connection info
        # if that changed (e.g. ip address)
        if protocol := self.get_protocol(protocol_id):
            # existing protocol object found,
            # this is a device that has been updated/reconnected
            # or simply a re-announcement on mdns.
            if cur_address and cur_address != protocol.device_info.ip_address:
                self.logger.debug(
                    "Address updated to %s for %s", cur_address, protocol.display_name
                )
            # publish any changes to the values the protocol object reports
            # note that you would normally call this from some other callback from
            # the device's native api/library which informs you of changes in its state.
            # as a last resort you can also choose to let the players controller
            # poll the protocol for state changes (needs_poll)
            protocol.update_state()
            return
        # handle new device
        self.logger.debug("Discovered device %s on %s", name, cur_address)
        # your own connection logic will probably be implemented here where
        # you connect to the device etc. using your device/provider specific library.
        # Before setting up a connection you can ask the player controller whether it wants
        # the protocol at all: not for a player the user disabled.
        # if not self.mass.players.wants_protocol(protocol_id):
        #     return

    async def discover_players(self) -> None:
        """Register every device this provider knows of that has no attached protocol."""
        # This is an optional method that you can implement if
        # you want to (manually) discover devices on the
        # network and you do not use mdns discovery.
        # It is called when the provider is (re)loaded and on every discovery request of the
        # players controller (a player of it enabled, removed or merged, for example), so it
        # must be safe to repeat.
        number_of_players = cast("int", self.config.get_value(CONF_NUMBER_OF_PLAYERS, 0))
        self.logger.info(
            "Discovering %s demo players",
            number_of_players,
        )
        for i in range(number_of_players):
            protocol_id = f"demo_{i}"
            if self.get_protocol(protocol_id):
                continue  # already attached
            protocol = DemoPlayer(provider=self, protocol_id=protocol_id)
            # register the protocol object: the player controller attaches it to the
            # player the device belongs to, or creates that player. Nothing is attached
            # for a player the user disabled; on_attach tells when it is attached.
            await self.mass.players.register(protocol)
            # once the protocol is attached, you can either let the players controller
            # poll it for state changes (needs_poll) or you can implement your own logic to listen for
            # state changes from the device and update the protocol object accordingly.
            # if a reported value changed, call the update method on the protocol:
            # protocol.update_state()
