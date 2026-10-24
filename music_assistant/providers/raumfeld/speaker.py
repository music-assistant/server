"""A hidden player for a speaker that plays as part of a Raumfeld room of several speakers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player

if TYPE_CHECKING:
    from .provider import RaumfeldPlayerProvider


class RaumfeldSpeaker(Player):
    """
    A speaker of a Raumfeld room of several speakers, other than the one the room is linked to.

    The room plays through its own player. This one only holds the speaker's own DLNA,
    Chromecast and Sendspin outputs, so they do not show up as players of their own, and
    is hidden. It plays nothing itself.
    """

    _attr_hidden_by_default = True
    _attr_expose_to_ha_by_default = False

    def __init__(
        self,
        provider: RaumfeldPlayerProvider,
        player_id: str,
        room: str,
        name: str,
        model: str | None,
        renderer_uuid: str,
        renderer_ip: str | None,
    ) -> None:
        """
        Initialize the speaker.

        :param provider: The Raumfeld provider.
        :param player_id: The speaker's player id.
        :param room: The Raumfeld room the speaker plays in.
        :param name: The speaker's own name.
        :param model: The speaker's model name, if known.
        :param renderer_uuid: The UUID of the speaker's media renderer.
        :param renderer_ip: The speaker's IP address, if known.
        """
        super().__init__(provider, player_id)
        self.room = room
        self._attr_name = name
        self._attr_available = True
        self._attr_device_info = DeviceInfo(model=model or "Raumfeld", manufacturer="Teufel")
        # the same identifiers as a room's player, so Music Assistant links the speaker's
        # DLNA/Chromecast/Sendspin outputs to this player instead of showing them on their own
        self._attr_device_info.add_identifier(IdentifierType.UUID, renderer_uuid)
        if renderer_ip:
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, renderer_ip)

    def set_room(self, room: str) -> None:
        """
        Follow the room the speaker plays in.

        :param room: The Raumfeld room name.
        """
        self.room = room
        self._attr_available = True
        self.update_state()

    def set_available(self, available: bool) -> None:
        """
        Set the availability of this speaker.

        :param available: Whether the speaker is currently reachable.
        """
        self._attr_available = available
        self.update_state()
