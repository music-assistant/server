"""
MusicAssistant Player Queues Controller.

Handles all logic to PLAY Media Items, provided by Music Providers to supported players.

It is loosely coupled to the MusicAssistant Music Controller and Player Controller.
A Music Assistant Player always has a PlayerQueue associated with it
which holds the queue items and state.

The PlayerQueue is in that case the active source of the player,
but it can also be something else, hence the loose coupling.
"""

from __future__ import annotations

from .controller import CONF_DEFAULT_ENQUEUE_SELECT_ARTIST, PlayerQueuesController

__all__ = ["CONF_DEFAULT_ENQUEUE_SELECT_ARTIST", "PlayerQueuesController"]
