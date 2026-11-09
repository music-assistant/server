"""
Base class for the protocol objects of a Player Provider.

Player Provider implementations subclass PlayerProtocol and look up their own objects through
the provider (`get_protocol`, `protocols`). Use `mass.players.get_player` for the user-facing
player: its state, controller commands, or a player of another provider.
"""

from music_assistant.models.player import Player

PlayerProtocol = Player
