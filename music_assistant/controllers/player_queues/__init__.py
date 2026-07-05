"""
MusicAssistant Player Queues Controller.

Handles all logic to PLAY Media Items, provided by Music Providers to supported players.

It is loosely coupled to the MusicAssistant Music Controller and Player Controller.
A Music Assistant Player always has a queue associated with it. The server holds the full queue
state in a `PlayerQueueData` record; the wire-facing `PlayerQueue` snapshot (the simplified view
shared with API clients) is normally the player's active source, but it can also be something else,
hence the loose coupling.
"""

from __future__ import annotations

from .controller import PlayerQueuesController

__all__ = ["PlayerQueuesController"]
