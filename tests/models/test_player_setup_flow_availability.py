"""Tests for hiding the reconfigure action when a setup flow has nothing to offer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from music_assistant.models.player import Player

if TYPE_CHECKING:
    from music_assistant_models.player import OutputProtocol

    from music_assistant.models.setup_flow import SetupSession


class _FlowPlayer(Player):
    """A player implementing a flow, so ``implements_setup_flow`` is True."""

    async def run_setup_flow(self, session: SetupSession) -> None:
        """Never runs; its presence is what marks the class as implementing a flow."""


class _WrapperPlayer(Player):
    """A player with no flow of its own, delegating to one non-native protocol child."""

    @property
    def output_protocols(self) -> list[OutputProtocol]:
        """Return the single stubbed child link this wrapper delegates to."""
        return self._stub_protocols


class _UnavailableFlowPlayer(_FlowPlayer):
    """A player whose flow currently has nothing left to set up."""

    @property
    def setup_flow_available(self) -> bool:
        """Report the flow as having nothing to offer."""
        return False


def test_a_player_implementing_a_flow_offers_it_by_default() -> None:
    """The base override answers True, so existing providers are unaffected."""
    player = _bare(_FlowPlayer)
    assert player.implements_setup_flow is True
    assert player.has_setup_flow is True


def test_a_player_can_report_its_flow_as_unavailable() -> None:
    """Reconfigure is hidden rather than opening a flow that can only abort."""
    player = _bare(_UnavailableFlowPlayer)
    assert player.implements_setup_flow is True
    assert player.has_setup_flow is False


def test_an_unavailable_child_flow_is_not_offered_through_its_wrapper() -> None:
    """A wrapper delegates to its protocol child, so it must respect the child's answer."""
    child = _bare(_UnavailableFlowPlayer)
    wrapper = _wrapper_over(child)
    assert wrapper.has_setup_flow is False


def test_an_available_child_flow_is_offered_through_its_wrapper() -> None:
    """The delegation still works for a child that does have something to set up."""
    child = _bare(_FlowPlayer)
    wrapper = _wrapper_over(child)
    assert wrapper.has_setup_flow is True


def _bare(cls: type[Player]) -> Player:
    """Construct a player without the full provider/mass wiring these reads do not need."""
    player = object.__new__(cls)
    player._cache = {}
    player._attr_player_id = "child-1"
    return player


def _wrapper_over(child: Player) -> _WrapperPlayer:
    """Return a player wrapping ``child`` as its single non-native output protocol."""
    wrapper = object.__new__(_WrapperPlayer)
    wrapper._cache = {}
    mass = MagicMock()
    mass.players.get_player = MagicMock(return_value=child)
    wrapper.mass = mass
    wrapper._stub_protocols = cast(
        "list[OutputProtocol]",
        [SimpleNamespace(is_native=False, output_protocol_id="child-1")],
    )
    return wrapper
