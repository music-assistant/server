"""Shared helpers for the config controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlayerFeature, PlayerType, ProtocolRole

from music_assistant.controllers.config.controller import ConfigController
from tests.common import (
    MockProtocol,
    MockProvider,
    attach_mock,
    mock_player_mass,
    use_player_registry,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player


def build_bare_config_controller(mock_mass: MagicMock) -> ConfigController:
    """
    Build a bare ConfigController wired to the given mocked mass.

    Mock setup must go through the caller's own ``mock_mass`` (not ``controller.mass``):
    ``ConfigController.mass`` is statically typed as ``MusicAssistant``, so reading it
    back would resolve real (non-mock) attribute/overload types instead of the mock's,
    even though the object assigned at runtime is the mock.

    :param mock_mass: Mocked Music Assistant instance to build the controller on.
    """
    controller = ConfigController.__new__(ConfigController)
    controller.mass = mock_mass
    return controller


def build_action_player(
    own_result: Any = None, audio_result: Any = None
) -> tuple[ConfigController, MockProtocol, MockProtocol]:
    """
    Build a bare ConfigController around a player whose protocols handle config actions.

    The player has the id ``player_1``, an own protocol of ``some_player_provider`` and
    an audio protocol ``protocol_1`` of ``protocol_provider``.

    :param own_result: What the config action of its own protocol returns.
    :param audio_result: What the config action of its audio protocol returns.
    """
    mock_mass = mock_player_mass()
    own_provider = MockProvider(
        "some_player_provider", instance_id="some_player_provider", mass=mock_mass
    )
    audio_provider = MockProvider(
        "protocol_provider", instance_id="protocol_provider", mass=mock_mass
    )
    player, own = attach_mock(own_provider, "own_1", "Player", player_id="player_1")
    _, audio = attach_mock(
        audio_provider, "protocol_1", "Player", role=ProtocolRole.AUDIO, player=player
    )
    own.handle_config_action = AsyncMock(return_value=own_result)  # type: ignore[method-assign]
    audio.handle_config_action = AsyncMock(  # type: ignore[method-assign]
        return_value=audio_result
    )
    return build_bare_config_controller(mock_mass), own, audio


def attach_on_config(
    mass: MusicAssistant,
    domain: str,
    protocol_id: str,
    name: str = "Player",
    *,
    role: ProtocolRole = ProtocolRole.NATIVE,
    player: Player | None = None,
    player_id: str | None = None,
    features: set[PlayerFeature] | None = None,
    player_type: PlayerType = PlayerType.PLAYER,
    protocol_class: type[MockProtocol] = MockProtocol,
) -> tuple[Player, MockProtocol]:
    """
    Attach a mock protocol to a player on a server with a real config controller.

    The provider of the protocol is registered on the server under its domain. The
    players controller is a stand-in that looks players and protocols up for real and
    takes config changes without acting on them; nothing is stored for the player or
    the protocol.

    :param mass: A server with a real config controller (the ``mass_minimal`` fixture).
    :param domain: Domain and instance id of the provider of the protocol.
    :param protocol_id: ID of the protocol object.
    :param name: The name its device reports.
    :param role: The role the protocol attaches in.
    :param player: The player to attach it to, for a second protocol of a device.
    :param player_id: Id of the player to create.
    :param features: The features the protocol reports, a volume control by default.
    :param player_type: The type of player the protocol reports.
    :param protocol_class: The class of the protocol.
    """
    if not isinstance(getattr(mass, "players", None), MagicMock):
        players = use_player_registry(mass)
        players.on_player_config_change = AsyncMock()
        players.on_protocol_config_change = AsyncMock()
        mass.player_queues = MagicMock()
        mass.player_queues.get.return_value = None
    provider = cast("MockProvider | None", mass.get_provider(domain))
    if provider is None:
        provider = MockProvider(domain, instance_id=domain, mass=cast("MagicMock", mass))
        provider.manifest.domain = domain
        provider.manifest.name = provider.name
        mass._providers[domain] = provider  # type: ignore[assignment]
        mass._provider_manifests[domain] = provider.manifest
    player, protocol = attach_mock(
        provider,
        protocol_id,
        name,
        role=role,
        player=player,
        player_id=player_id,
        player_type=player_type,
        protocol_class=protocol_class,
    )
    if features is not None:
        protocol._attr_supported_features = features
    player.refresh_state(signal_event=False)
    return player, protocol
