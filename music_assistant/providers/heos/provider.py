"""Demo Player Provider implementation."""

from __future__ import annotations

from pyheos import Credentials, Heos, HeosOptions

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.player_provider import PlayerProvider

from .player import HeosPlayer


class HeosPlayerProvider(PlayerProvider):
    """
    Example/demo Player provider.

    Note that this is always subclassed from PlayerProvider,
    which in turn is a subclass of the generic Provider model.

    The base implementation already takes care of some conveniencemethods,
    such as the mass object and the logger. Take a look at the base class
    for more information on what is available.

    Just like with any other subclass, make sure that if you override
    any of the default methods (such as __init__), you call the super() method.
    In most cases its not needed to override any of the builtin methods and you only
    implement the abc methods with your actual implementation.
    """

    _heos: Heos

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is initialized in Music Assistant.
        # you can use this to do any async initialization of the provider,
        # such as loading configuration, setting up connections, etc.
        self.logger.info("Initializing HeosPlayerProvider with config: %s", self.config)

        credentials: Credentials | None = None

        if (username := self.config.get_value(CONF_USERNAME)) is not None and (
            password := self.config.get_value(CONF_PASSWORD)
        ) is not None:
            credentials = Credentials(str(username), str(password))

        self._heos = Heos(
            HeosOptions(
                "192.168.50.207",
                credentials=credentials,
                auto_reconnect=True,
            )
        )

        # TODO: Handle connection failures
        await self._heos.connect()

        self.logger.info("Connected to HEOS System")

        players = await self._heos.get_players()

        self.logger.info("Found %s players", len(players))

        for player_id, player in players.items():
            self.logger.info(f"Found player {player_id}, {player.name}")

            heos_player = HeosPlayer(self, player)
            await heos_player.setup()

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called after the provider has been fully loaded into Music Assistant.
        # you can use this for instance to trigger custom (non-mdns) discovery of players
        # or any other logic that needs to run after the provider is fully loaded.
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
        for player in self.players:
            # if you have any cleanup logic for the players, you can do that here.
            # e.g. disconnecting from the player, closing connections, etc.
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

        await self._heos.disconnect()
