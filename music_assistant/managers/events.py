"""Logic to process events throughout the application."""


import logging
from typing import Any, Awaitable, Callable, Tuple, Union

from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import callback, create_task

LOGGER = logging.getLogger("eventbus")


class EventBus:
    """Global EventBus handling listening for and forwarding of events."""

    def __init__(self, mass: MusicAssistant):
        """Initialize EventBus instance."""
        self.mass = mass
        self._listeners = []

    @callback
    def signal(self, event_msg: str, event_details: Any = None) -> None:
        """
        Signal (systemwide) event.

            :param event_msg: the eventmessage to signal
            :param event_details: optional details to send with the event.
        """
        if self.mass.debug:
            LOGGER.debug("%s: %s", event_msg, str(event_details))
        for cb_func, event_filter in self._listeners:
            if not event_filter or event_msg in event_filter:
                create_task(cb_func, event_msg, event_details)

    @callback
    def add_listener(
        self,
        cb_func: Callable[..., Union[None, Awaitable]],
        event_filter: Union[None, str, Tuple] = None,
    ) -> Callable:
        """
        Add callback to event listeners.

        Returns function to remove the listener.
            :param cb_func: callback function or coroutine
            :param event_filter: Optionally only listen for these events
        """
        listener = (cb_func, event_filter)
        self._listeners.append(listener)

        def remove_listener():
            self._listeners.remove(listener)

        return remove_listener
