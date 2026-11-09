"""
Base class for the device objects (endpoints) of a Player Provider.

Player Provider implementations subclass PlayerEndpoint and look up their own objects through
the provider (`get_endpoint`, `endpoints`). Use `mass.players.get_player` for the user-facing
player: its state, core commands, or a player of another provider.
"""

from music_assistant.models.player import Player

PlayerEndpoint = Player
