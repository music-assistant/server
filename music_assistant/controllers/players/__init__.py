"""
MusicAssistant PlayerController.

Handles all logic to control supported players,
whose devices are provided by Player Providers.

Note that the PlayerController has a concept of a 'player', a 'protocol' and a 'playerstate'.
The Player is what users and clients see, one per device, and is owned by this controller.
A protocol is the object a provider registers for its device, which this controller
attaches to a player: it incorporates the (unaltered) state the device reports
(e.g. volume, state, etc) and the functions for controlling the device (e.g. play, pause, etc).

The playerstate is the (final) state of the player, derived from what its protocols
report, including any user customizations and transformations that are applied to it.
The playerstate is the object that is exposed to the outside world (via the API).
"""

from __future__ import annotations

from .controller import PlayerController

__all__ = ["PlayerController"]
