"""Tests for the PlayerProvider base class: its protocol lookups and its features."""

from __future__ import annotations

import logging
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderFeature, ProviderType
from music_assistant_models.provider import ProviderManifest

from music_assistant.models.player_provider import PlayerProvider
from tests.common import MockProtocol, capture_log_records

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
    """A lookup of another provider's protocol logs a warning and still returns the protocol."""
    mass = MagicMock()
    provider = _make_provider(mass, "test--own")
    other = _make_provider(mass, "test--other")
    protocol = MockProtocol(other, PLAYER_ID, "Kitchen")  # type: ignore[arg-type]
    mass.players.get_protocol.return_value = protocol

    with capture_log_records(provider.logger) as records:
        assert provider.get_protocol(PLAYER_ID) is protocol

    warnings = [record for record in records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "test--own" in message
    assert "test--other" in message
    assert PLAYER_ID in message
    # names the call site, so a misclassified lookup can be found
    assert f"called from {__name__}:" in message


def test_get_protocol_of_own_provider_does_not_warn() -> None:
    """A lookup of the provider's own protocol logs no warning."""
    mass = MagicMock()
    provider = _make_provider(mass, "test--own")
    protocol = MockProtocol(provider, PLAYER_ID, "Kitchen")  # type: ignore[arg-type]
    mass.players.get_protocol.return_value = protocol

    with capture_log_records(provider.logger) as records:
        assert provider.get_protocol(PLAYER_ID) is protocol

    assert not [record for record in records if record.levelno == logging.WARNING]


def test_get_protocol_of_mocked_player_does_not_warn() -> None:
    """A protocol with a mocked provider is not compared against the provider."""
    mass = MagicMock()
    provider = _make_provider(mass, "test--own")
    protocol = MagicMock()
    mass.players.get_protocol.return_value = protocol

    with capture_log_records(provider.logger) as records:
        assert provider.get_protocol(PLAYER_ID) is protocol

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


# Imports every player provider of the tree and prints how many provider classes that
# gave, followed by the name of each class that replaces the feature set of the base class.
_FIND_FEATURE_OVERRIDES = """
import importlib, json, pathlib
import music_assistant.providers as providers
from music_assistant.models.player_provider import PlayerProvider

for manifest in sorted(pathlib.Path(providers.__path__[0]).glob("*/manifest.json")):
    if json.loads(manifest.read_text())["type"] == "player":
        importlib.import_module(f"music_assistant.providers.{manifest.parent.name}")

def subclasses(cls):
    for subclass in cls.__subclasses__():
        yield subclass
        yield from subclasses(subclass)

classes = list(subclasses(PlayerProvider))
print(len(classes))
for cls in classes:
    if "supported_features" in vars(cls):
        print(cls.__name__)
"""


def test_no_player_provider_replaces_the_features_of_the_base_class() -> None:
    """Every player provider declares its features to the base class, which adds removal."""
    # in a fresh interpreter, so the imports do not touch the modules of other tests
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _FIND_FEATURE_OVERRIDES],
        capture_output=True,
        text=True,
        check=True,
    )
    class_count, *overriding = result.stdout.split()

    assert int(class_count) > 20
    assert overriding == []
