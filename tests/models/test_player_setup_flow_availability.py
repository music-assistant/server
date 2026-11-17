"""Tests for hiding the reconfigure action when a setup flow has nothing to offer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.models.player import Player
from music_assistant.models.player_protocol import PlayerProtocol

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


class _FlowProtocol(PlayerProtocol):
    """A protocol implementing a flow, so ``implements_setup_flow`` is True."""

    async def run_setup_flow(self, session: SetupSession) -> None:
        """Never runs; its presence is what marks the class as implementing a flow."""


class _UnavailableFlowProtocol(_FlowProtocol):
    """A protocol whose flow currently has nothing left to set up."""

    @property
    def setup_flow_available(self) -> bool:
        """Report the flow as having nothing to offer."""
        return False


def test_a_protocol_implementing_a_flow_offers_it_by_default() -> None:
    """The base override answers True, so existing providers are unaffected."""
    protocol = _bare(_FlowProtocol)
    assert protocol.implements_setup_flow is True
    assert protocol.has_setup_flow is True


def test_a_protocol_can_report_its_flow_as_unavailable() -> None:
    """Reconfigure is hidden rather than opening a flow that can only abort."""
    protocol = _bare(_UnavailableFlowProtocol)
    assert protocol.implements_setup_flow is True
    assert protocol.has_setup_flow is False


def test_an_unavailable_flow_is_not_offered_through_its_player() -> None:
    """A player offers the flows of its protocols, so it must respect their answer."""
    assert _player_of(_bare(_UnavailableFlowProtocol))._has_setup_flow is False


def test_an_available_flow_is_offered_through_its_player() -> None:
    """A player offers the flow of a protocol that does have something to set up."""
    assert _player_of(_bare(_FlowProtocol))._has_setup_flow is True


def _bare(cls: type[PlayerProtocol]) -> PlayerProtocol:
    """Construct a protocol without the full provider/mass wiring these reads do not need."""
    protocol = object.__new__(cls)
    protocol._cache = {}
    return protocol


def _player_of(protocol: PlayerProtocol) -> Player:
    """Return a player with the given protocol as its only one, without any other wiring."""
    player = object.__new__(Player)
    player._protocols = {"protocol_1": protocol}
    return player
