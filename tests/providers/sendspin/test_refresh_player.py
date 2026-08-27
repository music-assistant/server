"""Tests for the in-place player refresh and player class selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import PlayerType

import music_assistant.providers.sendspin.player as player_module
import music_assistant.providers.sendspin.provider as provider_module
from music_assistant.models.player import Player
from music_assistant.providers.sendspin.player import (
    SendspinBasePlayer,
    SendspinPlayer,
    SendspinSourcePlayer,
    SendspinVisualizerPlayer,
)
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player_provider import PlayerProvider


def _make_provider(player: SendspinBasePlayer | None) -> SendspinProvider:
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            players=SimpleNamespace(get_player=lambda _client_id: player),
            create_task=Mock(side_effect=AssertionError("refresh must not spawn tasks")),
        ),
    )
    return provider


def _mock_player(*, initialized: bool = True) -> Mock:
    player = Mock(spec=SendspinBasePlayer)
    player.initialized.is_set.return_value = initialized
    return player


async def test_refresh_player_updates_in_place() -> None:
    """A refresh re-applies config and state on the registered player object."""
    player = _mock_player()
    provider = _make_provider(player)
    await provider._refresh_player("c")
    player.on_config_updated.assert_awaited_once()
    player.update_state.assert_called_once()


async def test_refresh_player_ignores_unknown_player() -> None:
    """A refresh for a client without a registered player is a no-op."""
    provider = _make_provider(None)
    await provider._refresh_player("c")


async def test_refresh_player_ignores_uninitialized_player() -> None:
    """A refresh during initial registration is a no-op (registration applies config)."""
    player = _mock_player(initialized=False)
    provider = _make_provider(player)
    await provider._refresh_player("c")
    player.on_config_updated.assert_not_awaited()
    player.update_state.assert_not_called()


class _StubPlayer:
    def __init__(
        self,
        provider: SendspinProvider,
        client_id: str,
        initial_hello: object | None = None,
    ) -> None:
        self.player_id = client_id
        self._attr_type: PlayerType | None = None
        self._attr_underlying_player_id: str | None = None

    def preserve_control_features_from(self, other: object) -> None:
        pass


def _class_selection_provider(monkeypatch: pytest.MonkeyPatch) -> SendspinProvider:
    provider = SendspinProvider.__new__(SendspinProvider)
    provider._bridge_identifiers = {}
    provider._bridge_player_types = {}
    provider._bridge_underlying_players = {}
    provider._bridge_static_delay_defaults = {}
    monkeypatch.setattr(provider_module, "SendspinPlayer", _StubPlayer)
    monkeypatch.setattr(provider_module, "SendspinVisualizerPlayer", _StubPlayer)
    return provider


def _client(negotiated_role_ids: list[str]) -> SendspinClient:
    return cast(
        "SendspinClient",
        SimpleNamespace(
            negotiated_role_ids=negotiated_role_ids,
            roles_by_family=Mock(side_effect=AssertionError("must select on negotiated roles")),
        ),
    )


@pytest.mark.parametrize(
    ("negotiated_role_ids", "expected_type"),
    [
        (["player@v1", "metadata@v1"], None),
        (["metadata@v1"], PlayerType.DISPLAY),
        (["visualizer@v1"], PlayerType.VISUALIZER),
        ([], None),
    ],
)
def test_create_player_selects_class_on_negotiated_roles(
    monkeypatch: pytest.MonkeyPatch,
    negotiated_role_ids: list[str],
    expected_type: PlayerType | None,
) -> None:
    """The player class/type follows negotiated roles, not the (trust-gated) active roles."""
    provider = _class_selection_provider(monkeypatch)
    player = provider._create_player("c", _client(negotiated_role_ids), None)
    assert cast("_StubPlayer", player)._attr_type == expected_type


class _StubSourcePlayer(_StubPlayer):
    pass


def test_create_player_uses_the_capture_only_class_for_source_only_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source-only client must not become a player that advertises playback."""
    provider = _class_selection_provider(monkeypatch)
    monkeypatch.setattr(provider_module, "SendspinSourcePlayer", _StubSourcePlayer)
    source_only = provider._create_player("c", _client(["source@v1"]), None)
    assert isinstance(cast("object", source_only), _StubSourcePlayer)
    # A device that also plays keeps the full player class.
    combo = provider._create_player("c", _client(["source@v1", "player@v1"]), None)
    assert not isinstance(cast("object", combo), _StubSourcePlayer)


def test_player_classes_declare_their_privacy() -> None:
    """
    Only players owned by a single device or by the server itself are private.

    Visualizers and lights are hidden by default too, but clients keep offering
    them as group members, so they must not be marked private.
    """
    assert SendspinVisualizerPlayer._attr_hidden_by_default is True
    assert SendspinVisualizerPlayer._attr_private is False
    assert SendspinSourcePlayer._attr_private is True


def test_player_classes_advertise_grouping_by_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only Sendspin clients with an output role advertise grouping capability."""

    def _init_player(self: Player, provider: PlayerProvider, player_id: str) -> None:
        self._provider = provider
        self._player_id = player_id
        self._attr_can_group_with = set()

    client = SimpleNamespace(info=SimpleNamespace(player_support=None))
    provider = cast(
        "SendspinProvider",
        SimpleNamespace(
            instance_id="sendspin--test",
            logger=Mock(),
            server_api=SimpleNamespace(get_client=Mock(return_value=client)),
        ),
    )
    monkeypatch.setattr(Player, "__init__", _init_player)
    monkeypatch.setattr(SendspinBasePlayer, "_refresh_client_info", Mock())
    monkeypatch.setattr(SendspinBasePlayer, "_subscribe_client_callbacks", Mock())
    monkeypatch.setattr(SendspinPlayer, "_refresh_client_info", Mock())
    monkeypatch.setattr(SendspinPlayer, "_subscribe_client_callbacks", Mock())
    monkeypatch.setattr(player_module, "SendspinPlaybackSession", Mock())

    audio_player = SendspinPlayer(provider, "audio")
    visualizer_player = SendspinVisualizerPlayer(provider, "visualizer")
    source_player = SendspinSourcePlayer(provider, "source")

    assert audio_player.can_group_with == {provider.instance_id}
    assert visualizer_player.can_group_with == {provider.instance_id}
    assert source_player.can_group_with == set()
