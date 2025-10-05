"""
MusicAssistant PlayerController.

Handles all logic to control supported players,
which are provided by Player Providers.

Note that the PlayerController has a concept of a 'player' and a 'playerstate'.
The Player is the actual object that is provided by the provider,
which incorporates the actual state of the player (e.g. volume, state, etc)
and functions for controlling the player (e.g. play, pause, etc).

The playerstate is the (final) state of the player, including any user customizations
and transformations that are applied to the player.
The playerstate is the object that is exposed to the outside world (via the API).
"""

from __future__ import annotations

from .player_controller import PlayerController

__all__ = ["PlayerController"]
