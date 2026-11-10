"""Tests for the PlayerProvider base class: its protocol lookups and its features."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderFeature, ProviderType
from music_assistant_models.provider import ProviderManifest

from music_assistant.models.player_provider import PlayerProvider
from tests.common import MockPlayer, capture_log_records

PLAYER_ID = "test_player"


def _make_provider(
    mass: MagicMock, instance_id: str, supported_features: set[ProviderFeature] | None = None
) -> PlayerProvider:
    """
    Create a bare player provider on the given (mocked) Music Assistant instance.

    :param mass: The mocked Music Assistant instance.
    :param instance_id: Instance id of the provider.
    :param supported_features: The features the provider declares itself.
    """
    return PlayerProvider(
        mass,
        manifest=ProviderManifest(
            type=ProviderType.PLAYER,
            domain="test",
            name="Test",
            description="Test player provider",
            codeowners=["@music-assistant"],
        ),
        config=ProviderConfig(
            values={},
            type=ProviderType.PLAYER,
            domain="test",
            instance_id=instance_id,
            name="Test",
        ),
        supported_features=supported_features,
    )


def test_get_protocol_of_another_provider_warns() -> None:
    """A lookup of another provider's player logs a warning and still returns the player."""
    mass = MagicMock()
    provider = _make_provider(mass, "test--own")
    other = _make_provider(mass, "test--other")
    player = MockPlayer(other, PLAYER_ID, "Kitchen")  # type: ignore[arg-type]
    mass.players.get_player.return_value = player

    with capture_log_records(provider.logger) as records:
        assert provider.get_protocol(PLAYER_ID) is player

    warnings = [record for record in records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "test--own" in message
    assert "test--other" in message
    assert PLAYER_ID in message
    # names the call site, so a misclassified lookup can be found
    assert f"called from {__name__}:" in message


def test_get_protocol_of_own_provider_does_not_warn() -> None:
    """A lookup of the provider's own player logs no warning."""
    mass = MagicMock()
    provider = _make_provider(mass, "test--own")
    player = MockPlayer(provider, PLAYER_ID, "Kitchen")  # type: ignore[arg-type]
    mass.players.get_player.return_value = player

    with capture_log_records(provider.logger) as records:
        assert provider.get_protocol(PLAYER_ID) is player

    assert not [record for record in records if record.levelno == logging.WARNING]


def test_get_protocol_of_mocked_player_does_not_warn() -> None:
    """A player with a mocked provider is not compared against the provider."""
    mass = MagicMock()
    provider = _make_provider(mass, "test--own")
    player = MagicMock()
    mass.players.get_player.return_value = player

    with capture_log_records(provider.logger) as records:
        assert provider.get_protocol(PLAYER_ID) is player

    assert not [record for record in records if record.levelno == logging.WARNING]


@pytest.mark.parametrize(
    "declared",
    [None, {ProviderFeature.SYNC_PLAYERS}, {ProviderFeature.REMOVE_PLAYER}],
)
def test_every_player_provider_offers_the_removal_of_players(
    declared: set[ProviderFeature] | None,
) -> None:
    """A player provider advertises removal next to the features it declares itself."""
    provider = _make_provider(MagicMock(), "test", declared)

    assert provider.supported_features == {*(declared or ()), ProviderFeature.REMOVE_PLAYER}
    assert provider.supports_feature(ProviderFeature.REMOVE_PLAYER)
    assert provider.to_dict()["supported_features"].count("remove_player") == 1
