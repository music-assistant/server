"""Tests for hiding the reconfigure action when a setup flow has nothing to offer."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, PropertyMock, patch

from music_assistant.models.player import Player

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.models.setup_flow import SetupSession


class _FlowPlayer(Player):
    """A player implementing a flow, so ``implements_setup_flow`` is True."""

    async def run_setup_flow(self, session: SetupSession) -> None:
        """Never runs; its presence is what marks the class as implementing a flow."""


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
    with _wrapper_over(_bare(_UnavailableFlowPlayer)) as wrapper:
        assert wrapper.has_setup_flow is False


def test_an_available_child_flow_is_offered_through_its_wrapper() -> None:
    """The delegation still works for a child that does have something to set up."""
    with _wrapper_over(_bare(_FlowPlayer)) as wrapper:
        assert wrapper.has_setup_flow is True


def _bare(cls: type[Player]) -> Player:
    """Construct a player without the full provider/mass wiring these reads do not need."""
    player = object.__new__(cls)
    player._cache = {}
    return player


@contextmanager
def _wrapper_over(child: Player) -> Iterator[Player]:
    """
    Yield a player wrapping ``child`` as its single non-native output protocol.

    ``output_protocols`` derives its links from live config and player state, which is far
    more wiring than these reads need, so it is patched down to the one link under test.
    """
    wrapper = object.__new__(Player)
    wrapper._cache = {}
    mass = MagicMock()
    mass.players.get_player = MagicMock(return_value=child)
    wrapper.mass = mass
    links = [SimpleNamespace(is_native=False, output_protocol_id="child-1")]
    with patch.object(Player, "output_protocols", PropertyMock(return_value=links)):
        yield wrapper
